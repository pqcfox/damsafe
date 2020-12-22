import random
import sqlite3
import threading
import os
import time
from datetime import datetime
from pathlib import Path

import click
import humanize
from pymodbus.client.sync import ModbusTcpClient
from pymodbus.exceptions import ConnectionException
from pymodbus.pdu import ModbusExceptions
from filelock import FileLock
from flask import (
    Flask, render_template, redirect, url_for, request, g, flash
)
from flask.cli import with_appcontext

app = Flask(__name__)
app.secret_key = 'dev'

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/add', methods=['POST'])
def add():
    db = get_db()
    name = request.form['name']
    ip = request.form['ip']
    coil = request.form['coil']

    error = None
    if not name:
        error = 'Device name is required.'
    elif not ip:
        error = 'IP address is required.'
    elif not coil:
        error = 'Coil is required.'
    elif db.execute(
        'SELECT id FROM device WHERE name = ?', (name,)
    ).fetchone() is not None:
        error = 'Device name is already taken.'
    if error is None:
        with FileLock('db.lock'):
            db.execute(
                'INSERT INTO device (name, ip, coil) VALUES (?, ?, ?)',
                (name, ip, coil)
            )
            db.commit()
        return redirect(url_for('dashboard'))

    flash(error)
    return redirect(url_for('dashboard'))


@app.route('/remove', methods=['POST'])
def remove():
    db = get_db()
    with FileLock('db.lock'):
        name = request.form['name']
        db.execute('DELETE FROM device WHERE name = ?', (name,))
        db.commit()
    return redirect(url_for('dashboard'))


@app.route('/data', methods=['GET'])
def data():
    db = get_db()
    db_rows = db.execute('SELECT statid.*,ds.status AS status,ds.error AS error,dsup.time AS seen_time '
                         'FROM (SELECT dev.id,dev.name,dev.ip,dev.coil,MAX(ds.time) AS status_time '
                         'FROM device AS dev LEFT OUTER JOIN device_status AS ds ON ds.device_id = dev.id '
                         'GROUP BY dev.id) AS statid LEFT OUTER JOIN device_status AS ds '
                         'ON ds.device_id = statid.id AND ds.time = statid.status_time '
                         'LEFT OUTER JOIN device_status AS dsup '
                         'ON dsup.device_id = statid.id AND dsup.time = statid.status_time AND dsup.status = True').fetchall()
    g.device_rows = []
    for db_row in db_rows:
        if db_row['status'] is None:
            status = '...'
            uptime = '...'
            lastseen = '...'
        elif db_row['status'] == 1:
            status = 'up'
            if db_row['seen_time'] is None:
                uptime = 'just started'
            else:
                uptime = str(datetime.utcnow() - db_row['seen_time']).split('.')[0]
            lastseen = 'now'
        else:
            status = 'down'
            uptime = 'n/a'
            if db_row['seen_time'] is None:
                lastseen = 'never'
            else:
                lastseen = humanize.naturaldelta(datetime.utcnow() - datetime.strptime(db_row['seen_time'], '%Y-%m-%d %H:%M:%S'))

        error = 'none' if db_row['error'] is None else db_row['error']
        device_row = {
            'name':     db_row['name'],
            'ip':       db_row['ip'],
            'coil':     db_row['coil'],
            'error':    error,
            'status':   status,
            'uptime':   uptime,
            'lastseen': lastseen
        }
        g.device_rows.append(device_row)

    try:
        mtime = os.path.getmtime('server_alive')
        alive = (time.time() - mtime < 10)
    except FileNotFoundError:
        alive = False
    g.server_status = 'Alive' if alive else 'Dead'

    statustime = db.execute('SELECT MAX(time) AS time FROM device_status').fetchone()['time']
    g.last_status_check = 'Never' if statustime is None else humanize.naturaldelta(datetime.utcnow() - datetime.strptime(statustime, '%Y-%m-%d %H:%M:%S'))

    return render_template('data.html')


@app.route('/dashboard', methods=['GET'])
def dashboard():
    return render_template('dashboard.html')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            'damsafe.sqlite',
            detect_types=(sqlite3.PARSE_DECLTYPES |
                          sqlite3.PARSE_COLNAMES)
        )
        g.db.row_factory = sqlite3.Row

    return g.db


def close_db(e=None):
    db = g.pop('db', None)

    if db is not None:
        db.close()


@click.command('init-db')
@with_appcontext
def init_db_command():
    """Clear the existing data and create new tables."""
    db = get_db()
    with app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))
    click.echo('Initialized database.')


@click.command('server')
@with_appcontext
def server_command():
    """Run the modbus server."""
    db = sqlite3.connect(
        'damsafe.sqlite',
        detect_types=(sqlite3.PARSE_DECLTYPES |
                      sqlite3.PARSE_COLNAMES)
    )
    db.row_factory = sqlite3.Row

    while True:
        Path('server_alive').touch()
        device_rows = db.execute('SELECT * FROM device').fetchall()
        for row in device_rows:
            client = ModbusTcpClient(row['ip'])
            new_status = True
            error = 'none'
            try:
                result = client.read_coils(row['coil'], 1)
                try:
                    error_code = result.exception_code
                    error = ModbusExceptions.decode(error_code)
                except AttributeError:
                    pass
            except ConnectionException:
                new_status = False

            with FileLock('db.lock'):
                db.execute('INSERT INTO device_status (device_id, time, status, error)'
                           'VALUES (?, datetime("now"), ?, ?)',
                           (row['id'], new_status, error))
                db.commit()

        time.sleep(5)


app.teardown_appcontext(close_db)
app.cli.add_command(init_db_command)
app.cli.add_command(server_command)
