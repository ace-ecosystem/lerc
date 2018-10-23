
CREATE DATABASE lerc;
USE lerc;

CREATE TABLE clients (hostname VARCHAR(40), status enum('ONLINE','OFFLINE','UNKNOWN','UNINSTALLED'), install_date DATETIME, company_id INT(11), last_activity DATETIME, sleep_cycle INT(11) DEFAULT 900);

CREATE UNIQUE INDEX host_x ON clients(hostname);

CREATE TABLE company_mapping (name VARCHAR(128), id INT(11) NOT NULL AUTO_INCREMENT, PRIMARY KEY (id));
INSERT INTO company_mapping SET name='example';

GRANT ALL PRIVILEGES ON lerc . * TO 'lerc_user'@'localhost' IDENTIFIED BY 'password';

CREATE TABLE commands (hostname VARCHAR(40), operation enum('RUN','DOWNLOAD','UPLOAD','QUIT'), command VARCHAR(1024), command_id INT(11) NOT NULL AUTO_INCREMENT, file_position INT DEFAULT 0, filesize BIGINT, client_file_path VARCHAR(1024), server_file_path VARCHAR(1024), status enum('PENDING','COMPLETE','UNKNOWN','ERROR','PREPARING'), log_file_path VARCHAR(1024), analyst_file_path VARCHAR(1024), PRIMARY KEY (command_id));