#!/usr/bin/python3
#/opt/cRAT_server/cratenv/bin/python3

import os
import sys
import json
import logging
import configparser
from datetime import datetime
from flask_restful import Resource, Api
from flask import Flask, request, jsonify, stream_with_context, Response, make_response

# only used if we're runing the app natively
from werkzeug.serving import WSGIRequestHandler

import library.clientInstructions as ci
from library.database import db, operationTypes, cmdStatusTypes, clientStatusTypes, Commands, Clients


BASE_DIR = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = 'data/'
LOG_DIR = 'logs/'
ETC_DIR = 'etc/'
DIRECTORIES = [DATA_DIR, LOG_DIR, ETC_DIR]
# make sure all the directories exists that need to exist
for path in [os.path.join(BASE_DIR, x) for x in DIRECTORIES]:
    if not os.path.isdir(path):
        try:
            os.mkdir(path)
        except Exception as e:
            sys.stderr.write("ERROR: cannot create directory {0}: {1}\n".format(
                path, str(e)))
            sys.exit(1)

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, ETC_DIR, 'lerc_server.ini'))

# globals and config items
DEFAULT_CLIENT_DIR = config['lerc_server']['default_client_dir']
DEFAULT_SLEEP = int(config['lerc_server']['default_client_sleep'])
CHUNK_SIZE = int(config['lerc_server']['chunk_size'])
DB_server = config['lerc_server']['dbserver']
DB_user = config['lerc_server']['dbuser']
DB_userpass = config['lerc_server']['dbuserpass']
database_connect_string = "mysql+pymysql://{}:{}@{}/lerc".format(DB_user, DB_userpass, DB_server)

# To handle the edgecase between HTTP/1.1 and WSGI spec
UGLY_CHUNKSIZE = 1 # yep, 1 byte

# Declare app, api, and initilize db
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = database_connect_string
db.init_app(app)
api = Api(app)

@app.before_request
def handle_chunking():
    """
    Sets the "wsgi.input_terminated" environment flag, thus enabling
    Werkzeug to pass chunked requests as streams; this makes the API
    compliant with the HTTP/1.1 standard.  The server should set
    the flag, but this feature has not been implemented.
    """
    transfer_encoding = request.headers.get("Transfer-Encoding", None)
    if transfer_encoding == "chunked":
        request.environ["wsgi.input_terminated"] = True


# configure some logging
logging.basicConfig(format='[%(levelname)s] %(asctime)s - %(name)s - %(message)s',
                    filename=os.path.join(BASE_DIR, LOG_DIR, 'server.log'))
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('root').setLevel(logging.WARNING)

class RequestFormatter(logging.Formatter):
    def format(self, record):
        record.url = request.url
        record.path = request.path
        record.full_path = request.full_path
        record.remote_addr = request.remote_addr
        return super().format(record)

FORMAT = RequestFormatter('[%(levelname)s] %(asctime)s - %(name)s - %(remote_addr)s %(full_path)s - %(message)s')
logger = logging.getLogger('lerc_server')
logger.propagate = False
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setFormatter(FORMAT)
fh = logging.FileHandler(os.path.join(BASE_DIR, LOG_DIR, 'server.log'))
fh.setFormatter(FORMAT)
logger.addHandler(handler)
logger.addHandler(fh)


# begin helper functions #
class CustomRequestHandler(WSGIRequestHandler):
    # Just to log connections dropped when streaming a response
    def connection_dropped(self, error, environ=None):
        logging.warning('Connection dropped when streaming response data to client.')

def host_check(host, company):
    # known host?
    client = Clients.query.filter_by(hostname=host).first()
    if client is None:
        new_client = Clients(host, company_id=company, sleep_cycle=15)
        db.session.add(new_client)
        db.session.commit()
        logger.info("Added {}/{} to client table".format(host, company))
    elif client.status == clientStatusTypes.UNINSTALLED:
        logger.info("Client being re-installed.")
        client.status = clientStatusTypes.ONLINE
        client.install_date = datetime.now()
        client.last_activity = datetime.now()
        client.company_id=company
        client.sleep_cycle=15
        db.session.commit()
    elif client.company_id != company:
        if client.company_id is None:
            logger.info("setting company_id for {}".format(host))
            client.company_id = company
            client.status = clientStatusTypes.ONLINE
            client.last_activity = datetime.now()
            db.session.commit()
            return True
        logger.error("Company id mismatch for {}.".format(host))
        client.status = clientStatusTypes.UNKNOWN
        db.session.commit()
        return False
    else:
        client.status = clientStatusTypes.ONLINE
        client.last_activity = datetime.now()
        db.session.commit()
    return True

def command_manager(host, remove_cid=None):
    if remove_cid:
        command = Commands.query.filter_by(hostname=host, command_id=remove_cid).one()
        db.session.delete(command)
        db.session.commit()
        logger.info("Removed command id:{} for host:{}".format(remove_cid, host))
        return True
    # Any open commands?
    command = Commands.query.filter(Commands.hostname==host).filter(Commands.status==cmdStatusTypes.PENDING).order_by(Commands.command_id.asc()).first()
    if command:
        if command.operation == operationTypes.UPLOAD:
            if command.server_file_path is None:
                filename = command.client_file_path
                filename = filename[filename.rfind('\\')+1:]
                command.server_file_path = "{}{}_{}_{}".format(DATA_DIR, command.hostname, command.command_id, filename)
                db.session.commit()
            elif os.path.exists(os.path.join(BASE_DIR,command.server_file_path)):
                # handling edge case where db didn't get updated correctly after data transfer
                statinfo = os.stat(os.path.join(BASE_DIR,command.server_file_path))
                if statinfo.st_size > (command.file_position - 1) and statinfo.st_size != 0:
                    command.file_position = statinfo.st_size + 1
                    logger.warning("Updating database: More bytes found on server than recorded in database. Prior exception went unhandled or unnoticed. Probable connection drop and resume before server reached timeout")
                    db.session.commit()
        elif command.operation == operationTypes.DOWNLOAD:
            if '/' not in command.client_file_path:
                ''' lerc.exe's writed files to c:\windows\system32 by defaut, so, change to DEFAULT_CLIENT_DIR
                    if not specified by the analyst when the command was issued '''
                command.client_file_path = DEFAULT_CLIENT_DIR + command.client_file_path
                db.session.commit()
                    
        return command
    return None


def receive_streamed_file(command):
    # Write a file to the server - return None on Sucess, else error message
    stream_error = None
    chunk_size = CHUNK_SIZE
    # to make up for the design limitation of Werkzeug/wsgi_mod for streaming chunked data
    # gonna do this hack
    total_chunks, remaining_bytes = divmod(command.filesize - command.file_position, chunk_size)
    with open(os.path.join(BASE_DIR,command.server_file_path), 'ba') as f:
        for i in range(total_chunks):
            try:
                chunk = request.stream.read(chunk_size)
                # leaving the following len check in for native mode
                if len(chunk) == 0:
                    break
                f.write(chunk)
            except Exception as e:
                 stream_error = type(e).__name__ + " - " + str(e)
                 break
        try:
            final_chunk = request.stream.read(remaining_bytes)
            f.write(final_chunk)
        except OSError as e:  #request data read error
            stream_error = "Stream closed pre-maturely. ({})".format(str(e))
        f.close()
    return stream_error
# End helper functions #


# Begin server API for client resources
class Fetch(Resource):
    def get(self):
        if 'host' not in request.args:
            logger.error("Malformed request")
            return None
        if 'company' not in request.args:
            logger.error("Malformed request")
            return None

        host = request.args['host']
        company = request.args['company']
        if not host_check(host, int(company)):
            # if something is not right, always issue sleep
            return ci.Sleep(DEFAULT_SLEEP)
        command = command_manager(host)

        if command:
            logger.info("Issuing {} to {}".format(command.operation, command.hostname))
            if command.operation == operationTypes.RUN:
                return ci.Run(command.command_id, command.command)
            elif command.operation == operationTypes.UPLOAD:
                return ci.Upload(command.command_id, command.client_file_path, command.file_position)
            elif command.operation == operationTypes.DOWNLOAD:
                return ci.Download(command.command_id, command.client_file_path)
            elif command.operation == operationTypes.QUIT:
                command.status = cmdStatusTypes.COMPLETE
                client = Clients.query.filter_by(hostname=host).first()
                client.status = clientStatusTypes.UNINSTALLED
                db.session.commit()
                return ci.Quit()

        # default: tell client to do nothing
        logger.info("Issuing Sleep({}) to {}".format(DEFAULT_SLEEP, host))
        return ci.Sleep(DEFAULT_SLEEP)


class Pipe(Resource):
    def post(self):
        if 'host' not in request.args:
            logger.error("Malformed pipe request")
            return None
        elif 'id' not in request.args:
            logger.error("Missing command id in pipe post from {}".format(request.args['host']))
            return None

        cid = request.args['id']
        host = request.args['host']
        command = Commands.query.filter_by(hostname=host, command_id=cid).one()
        logger.info("Receiving Run command result from {}: ".format(host))
        command.server_file_path = "{}{}_RUN_{}".format(DATA_DIR, host, cid)

        with open(os.path.join(BASE_DIR,command.server_file_path), 'bw') as f: 
            chunk_size = UGLY_CHUNKSIZE
            while True:
                try:
                    chunk = request.stream.read(chunk_size)
                    # len(chunk) left for running in native mode
                    if len(chunk) == 0:
                        break
                    f.write(chunk)
                except OSError as e:
                    if 'request data read error' in str(e):
                        # we read one byte past the end of the stream, so we're done.
                        logger.debug("All available data collected from stream.")
                    else:
                        logger.error(str(e))
                    break
                except Exception as e:
                    if 'request data read error' in str(e):
                        # we read one byte past the end of the stream, so we're done.
                        logger.debug("{} - Read all data on the stream.".format(type(e).__name__))
                        break 
                    logger.error("Exception of type receiving data from {} :{} - {}".format(host, type(e).__name__, str(e)))
                    logger.warning("Command Status set to UNKNOWN for command {}".format(cid))
                    command.status = cmdStatusTypes.UNKNOWN
                    break

        # Assuming command completion if the state is still PENDING
        if command.status == cmdStatusTypes.PENDING:
            command.status = cmdStatusTypes.COMPLETE
            statinfo = os.stat(os.path.join(BASE_DIR, command.server_file_path))
            command.filesize = statinfo.st_size
            logging.info("Command status set to COMPLETE for command {}".format(cid))
        logger.info("Received data written to '{}'".format(command.server_file_path))
        db.session.commit()
        return True


class Upload(Resource):
    def post(self):
        if 'host' not in request.args:
            logger.error("Malformed Upload request")
            return None
        elif 'id' not in request.args:
            logger.error("Missing command id in upload post from {}".format(request.args['host']))
            return None

        cid = request.args['id']
        host = request.args['host']

        # get status of this command
        command = Commands.query.filter_by(hostname=host, command_id=cid).one()
        if command.filesize is None and 'size' in request.args:
            command.filesize = request.args['size']
            db.session.commit()
        if command.file_position > 0:
            logger.info("Resuming upload with {} at byte {}".format(host, command.file_position))
        else:
            logger.info("Receiving Upload result from {} for command {}".format(host, cid))

        stream_error = receive_streamed_file(command)

        # validating completion
        statinfo = os.stat(os.path.join(BASE_DIR,command.server_file_path))
        if stream_error is not None:
            # update the file position so the upload can be resumed
            command.file_position = statinfo.st_size + 1
            db.session.commit()
            logger.warning("Upload command {} Exception : '{}' | -> {} out of {} bytes received. Resuming on next fetch from host.".format(
                                                              command.command_id, stream_error, statinfo.st_size, command.filesize))
        elif statinfo.st_size == command.filesize or statinfo.st_size+1 == command.filesize:
            # Note that sometimes large files on unix show up a byte less in size than on NTFS windows
            logger.info("Upload command {} completed sucessfully - file: {}".format(command.command_id,
                                                                                    command.server_file_path))
            command.status = cmdStatusTypes.COMPLETE
            db.session.commit()
        else:
            logger.error("{} indicates upload command {} complete, however, {}/{} filesize mismatch".format(host, command.command_id,
                                                                                                  statinfo.st_size, command.filesize))
            command.status = cmdStatusTypes.UNKNOWN
            db.session.commit()
        return True


class Download(Resource):
    def get(self):
        if 'host' not in request.args or 'id' not in request.args or 'position' not in request.args:
            logger.error("Malformed Download request")
            return None

        cid = request.args['id']
        host = request.args['host']

        logger.info("Sending Download to {} for command {}".format(host, cid))
        
        command = Commands.query.filter_by(hostname=host, command_id=cid).one()
        command.file_position = int(request.args['position'])
        try:
            statinfo = os.stat(os.path.join(BASE_DIR, command.server_file_path))
        except FileNotFoundError as e:
            logger.error("FileNotFoundError: {} on this server".format(str(e)))
            command.status = cmdStatusTypes.ERROR
            command.log_file_path = "FileNotFoundError"
            db.session.commit()
            return Response()
        # This should really only detect when someone runs the exact same Download command
        if command.file_position == statinfo.st_size:
            logger.warning("File at same path and of same size is already on {}. Previously repeated command?".format(host))
            # Do we want to note this back to the analyst?
            command.status = cmdStatusTypes.COMPLETE
            db.session.commit()
            logger.info("Download command {} completed successfully".format(command.command_id))
            return Response()

        def stream_response():
            error_message = None
            chunk_size = CHUNK_SIZE
            try:
                with open(os.path.join(BASE_DIR, command.server_file_path), 'rb') as f:
                    f.seek(command.file_position)
                    data = f.read(chunk_size)
                    while data:
                        yield data
                        data = f.read(chunk_size)
            except Exception as e:
                error_message = str(e)

            if error_message is None:
                command.status = cmdStatusTypes.COMPLETE
                db.session.commit()
                logger.info("Download command {} completed successfully".format(command.command_id))
            else:
                logger.warning("Exception encountered for Download command {} : '{}'".format(
                                                                           command.command_id,
                                                                           error_message))

        #return Response(stream_response())
        return Response(stream_with_context(stream_response()))


class Error(Resource):
    def post(self):
        if 'host' not in request.args:
            logger.error("Malformed Error request")
            return None
        elif 'id' not in request.args:
            logger.error("Missing command id in Error post from {}".format(request.args['host']))
            return None
        cid = request.args['id']
        host = request.args['host']
        error_message = ""
        chunk_size = UGLY_CHUNKSIZE
        while True:
            try:
                chunk = request.stream.read(chunk_size)
                # leaving for native mode (werkzeug.streaming)
                if len(chunk) == 0:
                    break
                error_message = error_message + chunk.decode("utf-8")
            except OSError as e:
                # We tried to read a byte past the stream, we're done.
                break
            except Exception as e:
                logger.error("An Exception of type {} occured when receiving error from {}: {}".format(type(e).__name__,
                                                                                                       host, str(e)))
                break
        # logic to record command completion
        command = Commands.query.filter_by(hostname=host, command_id=cid).one()
        command.log_file_path = "{}{}_{}_ERROR.log".format(LOG_DIR, host, cid)
        command.status = cmdStatusTypes.ERROR
        db.session.commit()
        error_log = {'time': str(datetime.now()),
                     'command_id': str(command.command_id),
                     'host': str(host),
                     'operation': str(command.operation.name),
                     'server file path': str(command.server_file_path),
                     'client file path': str(command.client_file_path),
                     'command': str(command.command),
                     'error': str(error_message)}
        with open(os.path.join(BASE_DIR, command.log_file_path), 'w') as f:
            json.dump(error_log, f)
        logger.error("Error message from host={} for command_id={} : '{}'".format(host, cid, error_message))
        return False
# end client api resources


# begin analyst management api resource definitions
class Command(Resource):
    def post(self):
        # for posting new client commands and resources
        def custom_response(status_code, message, command_id=None, position=None):
            return {'status_code': status_code,
                    'message': message,
                    'command_id': command_id}

        if 'host' not in request.args:
            logger.error("Malformed Error request")
            return make_response("Missing host argument", 400)

        host = request.args['host']
        # make sure host exists in client tabel
        client = Clients.query.filter_by(hostname=host).first()
        if not client:
            return {'status_code':'404',
                    'message': "Not Found",
                    'error': "No LERC client installed on a host by name '{}'.".format(host)}

        if 'detach' in request.args:
            # set the client's sleep time back to default
            client.sleep_cycle = DEFAULT_SLEEP
            db.session.commit()
            logger.debug("Analyst detched from '{}'. Set client back to default sleep cycle".format(host))
            return custom_response(200, 'client {} set to default sleep')

        if not request.is_json:
            logger.error("Command request is not json")
            return make_response("Command request is not json", 400)

        host = request.args['host']
        command = request.json

        logger.info("Receiving Analyst command for {}".format(host))

        if command['operation'].upper() == operationTypes.RUN.name:
            new_command = Commands(host, operationTypes.RUN, command=command['command'])
            db.session.add(new_command)
            db.session.commit()
            logger.info("RUN command id {} created for {}".format(new_command.command_id, host))
            return custom_response(200, 'Created run command', command_id=new_command.command_id)
        elif command['operation'].upper() == operationTypes.UPLOAD.name:
            new_command = Commands(host, operationTypes.UPLOAD, client_file_path=command['client_file_path'])
            db.session.add(new_command)
            db.session.commit()
            logger.info("UPLOAD command id {} created for {}".format(new_command.command_id, host))
            return custom_response(200, 'created upload command', command_id=new_command.command_id)
        elif command['operation'].upper() == operationTypes.DOWNLOAD.name:
            new_command = Commands(host, operationTypes.DOWNLOAD,
                                   client_file_path=command['client_file_path'],
                                   analyst_file_path=command['analyst_file_path'],
                                   server_file_path=DATA_DIR+command['server_file_path'])
            db.session.add(new_command)
            db.session.commit()
            logger.info("DOWNLOAD command id {} created for {}".format(new_command.command_id, host))
            return custom_response(200, 'created download command', command_id=new_command.command_id)
        elif command['operation'].upper() == operationTypes.QUIT.name:
            new_command = Commands(host, operationTypes.QUIT)
            db.session.add(new_command)
            db.session.commit()
            logger.info("QUIT command id {} created for {}".format(new_command.command_id, host))
            return custom_response(200, 'created quit command', command_id=new_command.command_id)

        return None 

    def get(self):
        # command status and result retrival
        host = None
        if 'host' not in request.args and 'cid' not in request.args:
                logger.error("Malformed command request")
                return make_response("Missing arguments", 400)
        if 'cid' not in request.args and 'host' in request.args:
            # if only host argument, return host and host's command queue
            logger.info("Sending analyst the command queue for host: {}".format(request.args['host']))
            client = Clients.query.filter_by(hostname=request.args['host']).first()
            if not client:
                logger.warn("No client by name '{}'".format(request.args['host']))
                return {'status_code':'404',
                        'message': "Not Found",
                        'error': "Client '{}' does not exist.".format(request.args['host'])}
            commands = Commands.query.filter_by(hostname=request.args['host'])
            return {'commands': [ command.to_dict() for command in commands ],
                    'client': client.to_dict()}
        # else either cid and host in args or only cid in args. cid in args
        
        cid = request.args['cid']
        logger.debug("Analyst checking on command {}".format(cid))
        command = Commands.query.filter_by(command_id=cid).first()
        if not command:
            return {'status_code':'404',
                    'message': "Not Found OR Gone",
                    'error': "Command id '{}' does not exist.".format(cid)}

        # update the host to check in more frequntly during this analyst session
        client = Clients.query.filter_by(hostname=command.hostname).one()
        if client.status == clientStatusTypes.UNKNOWN:
            return {'status_code': '409',
                    'error': 'Client state UNKNOWN. Review Server logs.',
                    'message': 'Conflict'}

        if client.sleep_cycle != 30:
            client.sleep_cycle = 30
            db.session.commit() 
            logger.info("Updated host '{}' sleep cycle to 30 seconds for analyst session.".format(client.hostname))

        if command.status == cmdStatusTypes.PREPARING and command.operation == operationTypes.DOWNLOAD:
            # Update file position as needed
            if os.path.exists(os.path.join(BASE_DIR,command.server_file_path)):
                statinfo = os.stat(os.path.join(BASE_DIR,command.server_file_path))
                command.file_position = statinfo.st_size
                db.session.commit()
        elif command.status == cmdStatusTypes.ERROR:
            result = command.to_dict()
            with open(os.path.join(BASE_DIR, command.log_file_path), 'r') as f:
                error_log = json.load(f)
            result['error'] = error_log['error']
            result['time'] = error_log['time']
            logger.debug(result['error'], result['time'])
            return result

        return command.to_dict()


class AnalystUpload(Resource):
    def post(self):
        host = request.args['host']
        cid = request.args['cid']
        command = Commands.query.filter_by(hostname=host, command_id=cid).first()
        if not command:
            return {'status_code':'404',
                    'message': "Not Found", 
                    'error': "Command id '{}' does not exist.".format(cid)}
        command.filesize = int(request.args['filesize'])

        # see if a file by the same name and size already exists
        if os.path.exists(os.path.join(BASE_DIR,command.server_file_path)):
            statinfo = os.stat(os.path.join(BASE_DIR,command.server_file_path))
            if command.filesize == statinfo.st_size:
                command.status = cmdStatusTypes.PENDING
                db.session.commit()
                logger.warn("File by same name and size already exists")
                command_dict = command.to_dict()
                command_dict['warn'] = "File by same name and size already exists on server at {}".format(command.server_file_path)
                return command_dict

        stream_error = receive_streamed_file(command)

        # validating completion
        statinfo = os.stat(os.path.join(BASE_DIR,command.server_file_path))
        if stream_error is not None:
            # update the file position so the upload can be resumed
            command.file_position = statinfo.st_size + 1
            db.session.commit()
            logger.warning("Upload command {} Exception : '{}' | -> {} out of {} bytes received. Resuming on next fetch from host.".format(
                                                              command.command_id, stream_error, statinfo.st_size, command.filesize))
        elif statinfo.st_size == command.filesize or statinfo.st_size+1 == command.filesize:
            # Note that sometimes large files on unix show up a byte less in size than on NTFS windows
            logger.info("Sucessfully received file '{}' from analyst. Command now pending client fetch.".format(command.server_file_path))
            command.status = cmdStatusTypes.PENDING
            db.session.commit()
        else:
            logger.error("{} indicates upload command {} complete, however, {}/{} filesize mismatch".format(host, command.command_id,
                                                                                                  statinfo.st_size, command.filesize))
            command.status = cmdStatusTypes.UNKNOWN
            db.session.commit()

        return command.to_dict()

class AnalystDownload(Resource):
    def get(self):
        # stream results back to analyst
        def stream_results(command):
            chunk_size = CHUNK_SIZE
            logger.info("Streaming command {} results to analyst".format(command.command_id))
            try:
                with open(os.path.join(BASE_DIR, command.server_file_path), 'rb') as f:
                    f.seek(command.file_position)
                    data = f.read(chunk_size)
                    while data:
                        yield data
                        data = f.read(chunk_size)
            except Exception as e:
                logging.error(str(e))

        if 'cid' not in request.args:
            logger.warn("Malformed analyst download request")
            return {'status_code': '400',
                    'message': 'Bad Request',
                    'error': 'missing required arguments'}
        cid = request.args['cid']
        command = Commands.query.filter_by(command_id=cid).first()
        if not command:
            return {'status_code':'404',
                    'message': "Not Found",
                    'error': "Command id '{}' does not exist.".format(cid)}
        if command.status != cmdStatusTypes.COMPLETE:
            result = command.to_dict()
            result['warn'] = "Command is not COMPLETE."
            return result

        return Response(stream_with_context(stream_results(command)))
# end analyst management resource definitions

# add client api resources
api.add_resource(Fetch, '/fetch')
api.add_resource(Pipe, '/pipe')
api.add_resource(Download, '/download')
api.add_resource(Upload, '/upload')
api.add_resource(Error, '/error')

# add analyst api resources
api.add_resource(Command, '/command')
api.add_resource(AnalystUpload, '/command/upload')
api.add_resource(AnalystDownload, '/command/download')

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port='5000', request_handler=CustomRequestHandler)