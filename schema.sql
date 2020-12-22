DROP TABLE IF EXISTS device;
DROP TABLE IF EXISTS device_status;

CREATE TABLE device (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  ip TEXT NOT NULL,
  coil INT
);

CREATE TABLE device_status (
  device_id INTEGER,
  time TIMESTAMP,
  status BOOLEAN,
  error TEXT,
  PRIMARY KEY(device_id,time),
  FOREIGN KEY (device_id) REFERENCES device (id)
    ON DELETE CASCADE ON UPDATE CASCADE
);
