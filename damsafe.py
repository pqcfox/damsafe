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

# redirect to dashboard
@app.route('/')
def index():
    return redirect(url_for('dashboard'))


# add a device from the form on the dashboard
@app.route('/add', methods=['POST'])
def add():
    # get the database and all fields
    db = get_db()
    name = request.form['name']
    ip = request.form['ip']
    coil = request.form['coil']

    # determine if any fields are missing
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

    # if no error, take the database lock, insert new device
    # and redirect to dashboard
    if error is None:
        with FileLock('db.lock'):
            db.execute(
                'INSERT INTO device (name, ip, coil) VALUES (?, ?, ?)',
                (name, ip, coil)
            )
            db.commit()
        return redirect(url_for('dashboard'))

    # otherwise, flash the error and redirect to dashboard
    flash(error)
    return redirect(url_for('dashboard'))


# remove a route 
@app.route('/remove', methods=['POST'])
def remove():
    # get the database and database lock, remove device,
    # and redirect to dashboard
    db = get_db()
    with FileLock('db.lock'):
        name = request.form['name']
        db.execute('DELETE FROM device WHERE name = ?', (name,))
        db.commit()
    return redirect(url_for('dashboard'))


# form data table (reloaded dynamically on dashboard)
@app.route('/data', methods=['GET'])
def data():
    # get database and all devices
    db = get_db()
    db_rows = db.execute('SELECT statid.*,ds.status AS status,ds.error AS error,dsup.time AS seen_time '
                         'FROM (SELECT dev.id,dev.name,dev.ip,dev.coil,MAX(ds.time) AS status_time '
                         'FROM device AS dev LEFT OUTER JOIN device_status AS ds ON ds.device_id = dev.id '
                         'GROUP BY dev.id) AS statid LEFT OUTER JOIN device_status AS ds '
                         'ON ds.device_id = statid.id AND ds.time = statid.status_time '
                         'LEFT OUTER JOIN device_status AS dsup '
                         'ON dsup.device_id = statid.id AND dsup.time = statid.status_time AND dsup.status = True').fetchall()

    # for every database row, parse into plain english
    g.device_rows = []
    for db_row in db_rows:
        # if device not seen yet, wait
        if db_row['status'] is None:
            status = '...'
            uptime = '...'
            lastseen = '...'

        # if device is up, say so
        elif db_row['status'] == 1:
            status = 'up'
            # if we've not seen it before, it just started
            if db_row['seen_time'] is None:
                uptime = 'just started'
            # uptime is time last seen minus current time
            else:
                uptime = str(datetime.utcnow() - db_row['seen_time']).split('.')[0]
            lastseen = 'now'

        # otherwise, say it's down
        else:
            status = 'down'
            uptime = 'n/a'
            # if we've never seen it, say so
            if db_row['seen_time'] is None:
                lastseen = 'never'

            # otherwise time since last seen is current time minus last seen time
            else:
                lastseen = humanize.naturaldelta(datetime.utcnow() - datetime.strptime(db_row['seen_time'], '%Y-%m-%d %H:%M:%S'))

        # if no error, say so--otherwise get the error text
        error = 'none' if db_row['error'] is None else db_row['error']

        # add the row to the table
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
        # if the server is up, check when it last touched `server_alive`
        # if it's within the last 10 seconds, the server is alive
        mtime = os.path.getmtime('server_alive')
        alive = (time.time() - mtime < 10)
    except FileNotFoundError:
        # but if the file isn't there, it's definitely dead
        alive = False
    g.server_status = 'Alive' if alive else 'Dead'

    # get the last time a device was checked, and say how long ago that was
    statustime = db.execute('SELECT MAX(time) AS time FROM device_status').fetchone()['time']
    g.last_status_check = 'Never' if statustime is None else humanize.naturaldelta(datetime.utcnow() - datetime.strptime(statustime, '%Y-%m-%d %H:%M:%S'))

    # serve up the table
    return render_template('data.html')


# get dashboard
@app.route('/dashboard', methods=['GET'])
def dashboard():
    # render the dashboard
    return render_template('dashboard.html')


# get database
def get_db():
    # if g doesn't have the database, connect and set g.db to the database
    if 'db' not in g:
        g.db = sqlite3.connect(
            'damsafe.sqlite',
            detect_types=(sqlite3.PARSE_DECLTYPES |
                          sqlite3.PARSE_COLNAMES)
        )
        g.db.row_factory = sqlite3.Row

    return g.db


# close database
def close_db(e=None):
    # remove the database from g and close up shop
    db = g.pop('db', None)

    if db is not None:
        db.close()


# add init-db command for initializing database from schema
@click.command('init-db')
@with_appcontext
def init_db_command():
    """Clear the existing data and create new tables."""
    # get the database, apply the schema, and say we're done
    db = get_db()
    with app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))
    click.echo('Initialized database.')


# add server command for running modbus server
@click.command('server')
@with_appcontext
def server_command():
    """Run the modbus server."""
    # connect to the database
    db = sqlite3.connect(
        'damsafe.sqlite',
        detect_types=(sqlite3.PARSE_DECLTYPES |
                      sqlite3.PARSE_COLNAMES)
    )
    db.row_factory = sqlite3.Row

    while True:
        # let the Flask app know we're alive
        Path('server_alive').touch()

        # get all device data and loop over devices
        device_rows = db.execute('SELECT * FROM device').fetchall()
        for row in device_rows:
            # connect to the device
            client = ModbusTcpClient(row['ip'])
            new_status = True
            error = 'none'
            try:
                # try pinging the device
                result = client.read_coils(row['coil'], 1)
                try:
                    # if there's an error, record it
                    error_code = result.exception_code
                    error = ModbusExceptions.decode(error_code)
                except AttributeError:
                    pass
            except ConnectionException:
                # if we can't connect, say it's down
                new_status = False

            # record our findings in the device_status table
            with FileLock('db.lock'):
                db.execute('INSERT INTO device_status (device_id, time, status, error)'
                           'VALUES (?, datetime("now"), ?, ?)',
                           (row['id'], new_status, error))
                db.commit()

        # sleep to avoid overwhelming the devices
        time.sleep(5)


# ensure the app is linked to these
app.teardown_appcontext(close_db)
app.cli.add_command(init_db_command)
app.cli.add_command(server_command)
