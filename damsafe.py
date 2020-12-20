import sqlite3

import click
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

    error = None
    if not name:
        error = 'Device name is required.'
    elif not ip:
        error = 'IP address is required.'
    elif db.execute(
        'SELECT id FROM device WHERE name = ?', (name,)
    ).fetchone() is not None:
        error = 'Device name is already taken.'
    if error is None:
        db.execute(
            'INSERT INTO device (name, ip) VALUES (?, ?)',
            (name, ip)
        )
        db.commit()
        return redirect(url_for('dashboard'))

    flash(error)
    return redirect(url_for('dashboard'))


@app.route('/remove', methods=['POST'])
def remove():
    db = get_db()
    device_id = request.form['id']
    db.execute('DELETE FROM device WHERE id = ?', (device_id,))
    db.commit()
    return redirect(url_for('dashboard'))


@app.route('/dashboard', methods=['GET'])
def dashboard():
    db = get_db()
    g.device_rows = db.execute('SELECT * FROM device').fetchall()
    return render_template('dashboard.html')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            'damsafe.sqlite',
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


def close_db(e=None):
    db = g.pop('db', None)

    if db is not None:
        db.close()


def init_db():
    db = get_db()

    with app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))


@click.command('init-db')
@with_appcontext
def init_db_command():
    """Clear the existing data and create new tables."""
    init_db()
    click.echo('Initialized database.')


app.teardown_appcontext(close_db)
app.cli.add_command(init_db_command)
