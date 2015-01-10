# pybotz
A python interface to a subset of the web UI of APC's Netbotz 500 monitoring hardware [1].

Classes
=============================================================================
CheckerPool - simple pool of SensorModuleCheckers
SensorChecker - logic and state related to a single sensor
SensorModuleChecker - performance-oriented grouping of sensors to common 
                      network hosts
SensorReading - complex data type for data read from a sensor

Functions
=============================================================================
get_sensor_modules() - identify all modules on a given netbotz host
scrape_sensor_module() - get all readings from an identified sensor module

Terminology and Conceptual Organization of Netbotz Components
=============================================================================
Some familiarity with netbotz hardware is assumed here -- see [1] for more
background, if necessary.  

Each discrete Netbotz 500 unit is a "host" for the purposes of this module.
A given host can have a number of different physical components like cameras
and sensor pods attached; each of these is a "sensor module".  Each module
will provide one or more types of data (e.g. Temperature, Dew Point, etc. for
a "SensorPod 120"); each of these is referred to as a "sensor".

Required MySQL Schema
=============================================================================
This module expects to see a description of the netbotz environment it is 
scraping in a MySQL database with the following schema:

	 - - - - - start mysql schema - - - - -
	 
	CREATE SCHEMA IF NOT EXISTS `sensordata` DEFAULT CHARACTER SET latin1 ;
	USE `sensordata` ;
	
	-- -----------------------------------------------------
	-- Table `sensordata`.`host`
	-- -----------------------------------------------------
	CREATE  TABLE IF NOT EXISTS `sensordata`.`host` (
	  `id` INT NOT NULL AUTO_INCREMENT ,
	  `address` VARCHAR(45) NOT NULL COMMENT 'IP or hostname' ,
	  PRIMARY KEY (`id`) )
	ENGINE = InnoDB;
	
	-- -----------------------------------------------------
	-- Table `sensordata`.`sensor_module`
	-- -----------------------------------------------------
	CREATE  TABLE IF NOT EXISTS `sensordata`.`sensor_module` (
	  `id` INT UNSIGNED NOT NULL AUTO_INCREMENT ,
	  `host` INT UNSIGNED NOT NULL ,
	  `module_name` VARCHAR(45) NOT NULL ,
	  `track_data` TINYINT(1)  NOT NULL DEFAULT TRUE ,
	  `display_name` VARCHAR(45) NULL ,
	  PRIMARY KEY (`id`) )
	ENGINE = InnoDB, 
	COMMENT = 'Sensor modules are discrete pieces of netbotz hardware that ' /* comment truncated */ ;
	
	CREATE UNIQUE INDEX `host_mod_unique` ON `sensordata`.`sensor_module` (`host` ASC, `module_name` ASC) ;
	
	-- -----------------------------------------------------
	-- Table `sensordata`.`sensor`
	-- -----------------------------------------------------
	CREATE  TABLE IF NOT EXISTS `sensordata`.`sensor` (
	  `id` INT UNSIGNED NOT NULL AUTO_INCREMENT ,
	  `module` INT UNSIGNED NOT NULL ,
	  `sensor_name` VARCHAR(45) NOT NULL ,
	  `units` VARCHAR(45) NULL COMMENT 'Optionally store the appropriate type of units for this sensor reading (e.g. \"F\", \"ft/min\", \"%\", etc.)' ,
	  `track_data` TINYINT(1)  NOT NULL DEFAULT TRUE ,
	  `poll_interval` INT NULL COMMENT 'Defines how many seconds between regular updates of the given value.  \n\nIf 0, we update with every change in value.' ,
	  `alert_threshold` DECIMAL(3) NULL COMMENT 'Defines a threshold as a percentage of variance from the last value which, when exceeded, causes an immediate data update regardless of the poll_interval.\n\nThis value is ignored when poll_interval is 0.	' ,
	  PRIMARY KEY (`id`, `module`) )
	ENGINE = InnoDB;
	
	 - - - - - end mysql schema - - - - -

Tested Hardware
=============================================================================
Testing was done on an installation with two Netbotz 500 appliances and a 
combination of Camera Pod 120 and Sensor Pod 120 modules.  Other hardware 
details will probably work, perhaps with trivial changes to the code, but no
serious thought has been given to this.

Compatibility
=============================================================================
Tested on MacOS and Linux.  Signal handling won't work on Windows without
some changes.

[1] http://www.netbotz.com/products/appliances.html
