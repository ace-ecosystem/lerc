
import enum
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Begin DB models #
class operationTypes(enum.Enum):
    RUN  = 'RUN'
    UPLOAD = 'UPLOAD'
    DOWNLOAD = 'DOWNLOAD'
    QUIT = 'QUIT'

class cmdStatusTypes(enum.Enum):
    PENDING = 'PENDING'
    COMPLETE = 'COMPLETE'
    UNKNOWN = 'UNKNOWN'
    ERROR = 'ERROR'
    PREPARING = 'PREPARING' # file transfer between analysis <-> crat server

class clientStatusTypes(enum.Enum):
    ONLINE = 'ONLINE'
    OFFLINE = 'OFFLINE'
    UNKNOWN = 'UNKNOWN'
    UNINSTALLED = 'UNINSTALLED'

class Commands(db.Model):
    command_id = db.Column(db.Integer, primary_key = True)
    hostname = db.Column(db.String(40))
    operation = db.Column(db.Enum(operationTypes))
    command = db.Column(db.String(1024))
    file_position = db.Column(db.Integer)
    filesize = db.Column(db.Integer)
    client_file_path = db.Column(db.String(1024))
    server_file_path = db.Column(db.String(1024))
    status = db.Column(db.Enum(cmdStatusTypes))
    log_file_path = db.Column(db.String(1024))
    analyst_file_path = db.Column(db.String(1024))

    def __init__(self, hostname, operation, client_file_path=None,
                 server_file_path=None, command=None, analyst_file_path=None):
       self.hostname = hostname
       self.operation = operation
       self.client_file_path = client_file_path
       self.server_file_path = server_file_path
       self.analyst_file_path = analyst_file_path
       self.command = command
       if operation == operationTypes.DOWNLOAD:
           self.status = cmdStatusTypes.PREPARING
       else:
           self.status = cmdStatusTypes.PENDING
       self.file_position = 0
       self.filesize = None
       self.log_file_path = None

    def to_dict(self):
        return {'command_id': self.command_id,
                'hostname': self.hostname,
                'operation': self.operation.name,
                'client_file_path': self.client_file_path,
                'server_file_path': self.server_file_path,
                'command': self.command,
                'status': self.status.name,
                'file_position': self.file_position,
                'filesize': self.filesize,
                'log_file_path': self.log_file_path,
                'analyst_file_path': self.analyst_file_path}


class Clients(db.Model):
    hostname = db.Column(db.String(40), index=True, unique=True, primary_key = True)
    status = db.Column(db.Enum(clientStatusTypes))
    install_date = db.Column(db.DateTime)
    company_id = db.Column(db.Integer)
    last_activity = db.Column(db.DateTime)
    sleep_cycle = db.Column(db.Integer)

    def __init__(self, hostname, status=clientStatusTypes.ONLINE, install_date=datetime.now(), company_id=0, sleep_cycle=60):
        self.hostname = hostname
        self.status = status
        self.install_date = install_date
        self.company_id = company_id
        self.last_activity = datetime.now()
        self.sleep_cycle = sleep_cycle

    def to_dict(self):
        return {'hostname': self.hostname,
                'status': self.status.name,
                'install_date': self.install_date.strftime('%Y-%m-%d %H:%M:%S'),
                'company_id': self.company_id,
                'last_activity': self.last_activity.strftime('%Y-%m-%d %H:%M:%S'),
                'sleep_cycle': self.sleep_cycle}
# End DB models #
