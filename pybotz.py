#!/usr/bin/python

# Copyright 2012 Joshua Heling <jrh@netfluvia.org>
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

"""Classes and utility functions related to scraping data from the web UI 
of APC's Netbotz 500 monitoring hardware[1]. 

Classes
--------
CheckerPool - simple pool of SensorModuleCheckers
SensorChecker - logic and state related to a single sensor
SensorModuleChecker - performance-oriented grouping of sensors to common 
                      network hosts
SensorReading - complex data type for data read from a sensor

Functions
--------
get_sensor_modules() - identify all modules on a given netbotz host
scrape_sensor_module() - get all readings from an identified sensor module

Terminology and Conceptual Organization of Netbotz Components
----------------------------------
Some familiarity with netbotz hardware is assumed here -- see [1] for more
background, if necessary.  

Each discrete Netbotz 500 unit is a "host" for the purposes of this module.
A given host can have a number of different physical components like cameras
and sensor pods attached; each of these is a "sensor module".  Each module
will provide one or more types of data (e.g. Temperature, Dew Point, etc. for
a "SensorPod 120"); each of these is referred to as a "sensor".

Required MySQL Schema
----------------------------------
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
----------------------------------
Testing was done on an installation with two Netbotz 500 appliances and a 
combination of Camera Pod 120 and Sensor Pod 120 modules.  Other hardware 
details will probably work, perhaps with trivial changes to the code, but no
serious thought has been given to this.

Compatibility
----------------------------------
Tested on MacOS and Linux.  Signal handling won't work on Windows without
some changes.

[1] http://www.netbotz.com/products/appliances.html

"""

import sys
import re
import argparse
import time
import urllib2
import signal
from datetime import datetime, timedelta
import MySQLdb

from BeautifulSoup import BeautifulSoup

config = {}
"""Module-global dict of configuration settings."""

config['default_interval'] = 10 * 60     ## poll every 10 minutes
config['default_threshold'] = 0.50       ## alert immediately if a value changes 50%
config['self_report_interval'] = 15 * 60 ## report on poll stats every 15 minutes

class SensorReading:
  """Base class for a general sensor reading.
  
  Instance variables:
  ts
  
  Public methods:
  key()
  value()
  set()
  display_name()
  unit_string()  
  """

  _sensor_key = None
  _sensor_value = None

  ts = None;
  """Timestamp of the reading."""

  def __init__(self, timestamp, display_prefix=None):
    """Initialize a new SensorReading and parse initial HTML fragment.
    
    Arguments:
    timestamp -- the time associated with the reading
    display_prefix -- string to logically scope the sensor name for display (default None)
    """
    self.ts = timestamp
    if (display_prefix):
      self._display_prefix = display_prefix

  def __repr__(self):
    return "SensorReading _sensor_key:%s _sensor_value:%s" % \
      (self._sensor_key, self._sensor_value)

  def __str__(self):
    r_str = self._sensor_key + " = " + self._sensor_value + self.unit_string()
    return r_str

  def key(self):
    """Return key name (_sensor_key)."""
    ## This is a gratuitous accessor method, but is present for consistency with value()
    return self._sensor_key

  def value(self):
    """Return reading value."""
    if isinstance(self._sensor_value, float):
      return float(self._sensor_value)
    else:
      try:
          return int(self._sensor_value)
      except ValueError:
          try:
            return float(self._sensor_value)
          except ValueError:
            return (self._sensor_value)

  def set(self, key, value):
    """Manually set the key and value for a SensorReading."""
    """This is a utility function to enable a corner use case where we're just using SensorReading
    as a light wrapper datastructure.  If some usage pattern is going to frequently do this there
    are doubtless more elegant interfaces to offer."""
    self._sensor_key = key
    self._sensor_value = value

  def display_name(self):
    """Return key name for display."""
    if (self._display_prefix):
      return self._display_prefix + self._sensor_key
    else:
      return self._sensor_key

  def unit_string(self):
    """Return appropriate unit string."""
    if self._sensor_key in ("Temperature","Dew Point"):
      return " F"
    elif self._sensor_key == "Humidity":
      return " %"
    elif self._sensor_key == "Air Flow":
      return " ft/min"
    else:
      return ""

class NBSensorReading(SensorReading):
  """Represents a single reading of a given Netbotz sensor at a point in time.
  
  Parses the HTML produced by the Netbotz web UI and provies a few access methods.
  
  Public methods:
  load_from_HTML(frag)
  """
    
  _sensor_condition = None
  """Indicates if the current reading is in an alert state."""
  
  _display_prefix = None
  """A string which will be prepended to the _sensor_key for display purposes.
  This is useful when showing multiple sensors with the same name that are 
  connected to different hosts or modules."""
    
  def __init__(self, timestamp, htmlfrag, display_prefix=None):
    """Initialize a new NBSensorReading and parse initial HTML fragment.
    
    Arguments:
    timestamp -- the time associated with the reading
    htmlfrag -- portion of html (e.g. from BeautifulSoup) that contains the sensor data
    display_prefix -- string to logically scope the sensor name for display (default None)
    """
    SensorReading.__init__(self, timestamp, display_prefix)
    self.load_from_HTML(htmlfrag)

  def __repr__(self):
    return "NBSensorReading _sensor_key:%s _sensor_value:%s _sensor_condition:%s" % \
      (self._sensor_key, self._sensor_value, self._sensor_condition)
  
  def __str__(self):
    r_str = self._sensor_key + " = " + self._sensor_value + self.unit_string()
    if (self._sensor_condition):
      r_str += " (" + self._sensor_condition + ")"
    return r_str
      
  def load_from_HTML(self, frag):
    """Parse an HTML fragment to extract sensor data.
    
    Arguments:
    frag -- fragment of HTML (e.g. from BeautifulSoup)

    Notes:
    * Certain unit values are stripped from the reading and stored separately.
      Unrecognized units will not be stripped, and may confuse readings.
    * Obvious binary values are re-mapped to 0/1 (see code)
    * Spaces in key or value names are converted to '_'
    * Parens are stripped from key names
    """
    cells = frag.findAll("td")
    self._sensor_key = re.sub("\:$", "", cells[0].string)

    ## strip out units and decoration from value field - details depend on key
    if self._sensor_key in ('Temperature', 'Humidity', 'Dew Point', 'Air Flow', 'Audio'):
      # grab the leading numeric value 
      try:
        self._sensor_value = re.match(r"\d+\.?\d*", cells[1].a.string).group(0)
      except AttributeError:          ## disconnected sensor pods leave us with "N/A" values
        self._sensor_value = cells[1].a.string
    else:
      # treat everything else as a string
      ## (this has obvious limitations, and is a laziness enabled by a data coincidence)
      self._sensor_value = cells[1].a.string

    if cells[2].string == ('---'):
      self._sensor_condition = ""
    else:
      self._sensor_condition = cells[2].string

    self._sensor_key = re.sub(" ", "_", self._sensor_key)
    self._sensor_key = re.sub("\(","",self._sensor_key)      
    self._sensor_key = re.sub("\)","",self._sensor_key)
    self._sensor_value = re.sub(" ", "_", self._sensor_value)

    ## re-map certain non-numeric values to numerics for graphing
    if self._sensor_value in ("Closed", "No_Motion"):
      self._sensor_value = 0
    elif self._sensor_value in ("Open", "Motion_Detected"):
      self._sensor_value = 1

####################################
def get_sensor_modules(sensor_host):
  """Return a list of connected sensor units on a given netbotz sensor host."""

  r = []
  
  ## look for connected sensor units
  sensor_html = urllib2.urlopen(sensor_host + "/pages/menu_noscript.html").read()
  sensor_soup = BeautifulSoup(sensor_html)
  
  sensor_units = sensor_soup.findAll({'a' : True, 'target' : 'sensor'})
  for su in sensor_units:
    sensor = re.match(r"status.html\?encid=(.+)",su['href']).group(1)
    if sensor != "nbSensorSet_Alerting":
      r.append(sensor)
  
  return r

def scrape_sensor_module(sensor_host, sensor_module):
  """Return list of SensorReadings from a specified host / sensor unit.
  
  Arguments:
  sensor_host -- (string) hostname or IP of netbotz unit
  sensor_module -- name of the netbotz module to scrape.
  """
  page = urllib2.urlopen(sensor_host + "/pages/status.html?encid=" + sensor_module)

  html = page.read()
  reading_ts = datetime.now()
  soup = BeautifulSoup(html)

  outerTable = soup.findAll('table', limit=1)[0]

  ## the sensor unit label is bolded in the 2nd td tag with a trailing colon
  ## (fairly fragile) 
  nbSourceFrag = outerTable.findAll('td', limit=2)[1].b.contents
  nbSourceLabel = re.sub("\:$", "", nbSourceFrag[0].string)

  #print "------------------"
  #print "Scraping netbotz unit %s, sensor %s" % (nbSourceLabel, sensor_module)
  #print

  sensorTable = soup.find("table", "sensortable")

  #print sensorTable

  sensorRows = sensorTable.findAll("tr")

  sensorReadings = []
  for i in range(1, len(sensorRows)):
    r = NBSensorReading(reading_ts, sensorRows[i])
    sensorReadings.append(r)
    #print r

  return sensorReadings

class CheckerPool:
  """A simple collection of SensorModuleChecker instances.
  
  Public methods:
  check()
  """

  _SMC = None   
  """List of SensorModuleCheckers."""
  
  _dbh = None

  def __init__(self, dbh):
    """Create new CheckerPool tied to the given database.
    
    Arguments:
    dbh -- connected database handle to the db containing the sensor config
    """
    
    self._SMC = []
    self._dbh = dbh
    self._initialize_pool()
    
  def _initialize_pool(self):
    """Create SensorModuleChecker objects for each module defined in the database and add to
    the pool."""
    
    c = self._dbh.cursor()
    c.execute("""SELECT id, address FROM host""")  
    for row in c.fetchall():
      host = "http://" + row[1]
      hostid = row[0]
      c.execute("""SELECT module_name, display_name, id FROM sensor_module WHERE host = %s AND track_data = TRUE""", (hostid,))
      sensor_modules = c.fetchall()
      for (module_name, display_name, module_id) in sensor_modules:
        smc = SensorModuleChecker(host, module_name, display_name, module_id, self._dbh)
        self._SMC.append(smc)
    c.close()
    
  def check(self):
    """Check all sensors, return list of alerting SensorReadings."""
    new_alerts = []
    for smc in self._SMC:
      new_alerts.extend(smc.check()) 
    return new_alerts

class SensorModuleChecker:
  """A single "Sensor Module", which is a unit of Netbotz hardware for which 
  we get results.  
  
  Public methods:
  check()
  avg_poll_time()
  num_failures()
  num_successes()
  
  Public attributes:
  self_report_interval_len
  
  Note: A module may contain multiple different sensors, 
  but since we bear the majority of the retrieval cost in getting the HTML, 
  we group them together for performance reasons.
  """
  
  ## FIXMEs:
  ##
  ## * At most, db_id should be an optional argument (to save a db query) -- 
  ##   there's no reason we can't query for the id from here.
  
  self_report_interval = None
  """A timedelta object describing the interval (in seconds) between reports 
  on the speed and success rate of the sensor check.  
  Failure/success counters and average time are reset after each report."""
  
  _sensors = None
  _url = None
  _html = None 
  _html_ts = None
  _dbh = None  
  _host = None
  _module_name = None
  _display_name = None
  _db_id = None
  _read_timeout = 20
  _avg_poll_time = None
  _poll_failure_count = None
  _poll_success_count = None
  _next_self_report = None

  def __init__(self, host, module_name, display_name, db_id, dbh):
    """Initialize the SensorModule, including instantiating associated SensorCheckers.
    
    Arguments:
    host -- hostname or IP of the netbotz hardware
    module_name -- (string) netbotz name for the module
    display_name -- name used for display (may be more intelligible than netbotz' name)
    db_id -- id # of this module in the config database
    dbh -- connected database handle to the db containing the sensor config
    """
    self._sensors = []
    self._host = host
    self._module_name = module_name
    self._display_name = display_name
    self._db_id = db_id
    self._dbh = dbh
    self.self_report_interval = timedelta(0,config['self_report_interval'])
    
    self._url = self._host + "/pages/status.html?encid=" + self._module_name
    #print "DEBUG: instantiating SMC for %s (%s, %d)" % (self._module_name, self._display_name, self._db_id)
    #print "           url = %s" % self._url
    self._init_sensors()
    self._init_selfrpt_interval()
    #print "DEBUG: %d sensors found" % len(self._sensors)

  def _read_timeout_handler(self, signum, frame):
    raise IOError("Read timeout exceeded.")
    
  def _init_sensors(self):
    """Create sensor objects for all defined & enabled sensors."""
    #print "DEBUG: _init_sensors() for %s" % self._display_name
    c = self._dbh.cursor()
    c.execute("""SELECT id, sensor_name FROM sensor WHERE module = %s AND track_data = TRUE""", (self._db_id))
    sensors = c.fetchall()
    c.close()
    #print "DEBUG: %d sensors for %s" % (len(sensors), self._display_name)
    for (sensor_id, sensor_name) in sensors:
      s = SensorChecker(sensor_name, sensor_id, self._dbh)
      self._sensors.append(s)

  def _init_selfrpt_interval(self):
    """Reset counters and interval timer for self-reporting."""
    self._avg_poll_time = None
    self._poll_failure_count = 0
    self._poll_success_count = 0
    self._next_self_report = datetime.now() + self.self_report_interval
    
  def _record_poll_run(self,start,end):
    """Updates the running average poll time and success counter.  Takes start and end of a polling run."""
    self._poll_success_count += 1
    if self._avg_poll_time is None:
      self._avg_poll_time = end - start
    else:
      self._avg_poll_time = (self._avg_poll_time + (end - start)) / self._poll_success_count
      
    # print "DEBUG: average %s seconds to poll (%d/%d successful)" % (self._avg_poll_time, self._poll_success_count, 
    #                                                               self._poll_success_count + self._poll_failure_count)

  def _retrieve_HTML(self):
    """Retrieve the HTML containing this module's update.
    
    Side effects:
    - on success, increment success counter and add timing data to current running average
    - on failure (of any sort), increment failure counter
    """
    self._html = None
    
    start_time = datetime.now()
    try:
      page = urllib2.urlopen(self._url)
    except urllib2.URLError, e:
      print "Networking error: %s" % e
      self._poll_failure_count += 1
      self._html = None
      return
    
    ## read() will run forever if the connection gets flaky or goes away
    signal.signal(signal.SIGALRM, self._read_timeout_handler)
    signal.alarm(5)    
    try:
      self._html = page.read()
    except IOError:
      print "Read timeout."
      self._poll_failure_count += 1
      self._html = None
      return
    signal.alarm(0)
    
    self._html_ts = datetime.now()
    self._record_poll_run(start_time, self._html_ts)
  
  def _self_report(self):
    """Return SensorReadings for failure rates and average HTML poll time.
    
    Side effects: starts new self-report interval and resets counters
    """
    r = []
    
    total = (self._poll_success_count + self._poll_failure_count)
    if total > 0:
      failure_rate = SensorReading(datetime.now(), self._display_name + "-")
      failure_rate.set("poll_failure_rate", float(self._poll_failure_count / total))
      r.append(failure_rate)

    if self._poll_success_count > 0:
      avg_poll = SensorReading(datetime.now(), self._display_name + "-")
      avg_poll.set("avg_html_retrieval", self.avg_poll_time())
      r.append(avg_poll)

    self._init_selfrpt_interval()
    return r
    
  def check(self):
    """Check all sensors, return list of alerting SensorReadings."""
    new_alerts = []
    self._retrieve_HTML()

    if (self._html is None):
      print "HTML is null, skipping check."
      return new_alerts

    # parse the HTML to get updated sensor readings
    soup = BeautifulSoup(self._html)
    outerTable = soup.findAll('table', limit=1)[0]
    sensorTable = soup.find("table", "sensortable")
    sensorRows = sensorTable.findAll("tr")

    ## There may be sensor readings we don't care about parsed from the HTML, but we need to parse them all
    ##   to see what they are.
    ##
    ## NB:  this is a probably area for algorithmic efficiency improvement, but my gut is it's immaterial
    sensorReadings = {}
    for i in range(1, len(sensorRows)):
      try:
        r = NBSensorReading(self._html_ts, sensorRows[i], self._display_name + "-")
      except AttributeError:
        ## we get this if load_from_HTML fails
        continue
      sensorReadings[r.key()] = r
    
    for s in self._sensors:
      if not s.name() in sensorReadings:   ## need this in case the NBSensorReading instantiation above failed
        continue
      if (sensorReadings[s.name()]):            ## if we just got an update for this sensor
        if s.needs_check() or s.exceeds_threshold(sensorReadings[s.name()]):  ## ... and it's attention-worthy
          s.update(sensorReadings[s.name()])
          sr = s.get_data_update()
          if (sr):                              ##  ... and it is different than the last value
            new_alerts.append(sr)               ##  ... then alert on it.

    if datetime.now() > self._next_self_report:
      new_alerts.extend(self._self_report())

    return new_alerts

  def avg_poll_time(self):
    """Returns float from underlying timedelta."""
    try:
      return self._avg_poll_time.total_seconds()
    except AttributeError:  # total_seconds() is new in python 2.7
      td = self._avg_poll_time
      return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6
      
    
  def num_failures(self):
    return self._poll_failure_count
    
  def num_successes(self):
    return self._poll_success_count

class SensorChecker:
  """A single netbotz sensor.
  
  Public methods:
  exceeds_threshold()
  get_data_udpate()
  name()
  needs_check()
  update()
  """
  
  ## FIXMEs:
  ##
  ## * At most, db_id should be an optional argument (to save a db query) -- 
  ##   there's no reason we can't query for the id from here.
  
  ## Private attributes
  _sensor_name = None
  """The string by which netbotz knows the sensor."""""

  _module_name = None
  """The display name of the parent sensor module."""

  _db_id = None
  _dbh = None

  _next_check_time = None

  _poll_interval = None
  """Specified in seconds, stored as timedelta."""

  _alert_threshold = None
  """Sensor values which change by more than this percentage alert regardless of time."""
  
  _current_reading = None
  """A SensorReading object."""

  _previous_reading = None
  """A SensorReading object."""
  
  def __init__(self, sensor_name, db_id, dbh):
    """Initialize the sensor, setting up schedule & threshold based on config in the db.
    
    Arguments:
    sensor_name -- (string)
    db_id -- id # of this module in the config database
    dbh -- connected database handle to the db containing the sensor config
    """
    self._sensor_name = sensor_name
    self._db_id = db_id
    self._dbh = dbh
    self._next_check_time = datetime.now()  ## set to check initially

    c = self._dbh.cursor()
    c.execute("""SELECT poll_interval, alert_threshold FROM sensor WHERE id = %s AND track_data = TRUE""", (self._db_id))
    assert(c.rowcount == 1)
    (interval, threshold) = c.fetchone()
    c.close()
    
    if (interval is not None):
      self._poll_interval = timedelta(0,interval)
    else:
      self._poll_interval = timedelta(0,config['default_interval'])

    if (threshold is not None):
      self._alert_threshold = threshold
    else:
      self._alert_threshold = config['default_threshold']

  def name(self):
    """Return sensor name."""
    return self._sensor_name

  def needs_check(self):
    """Return True if this sensor is due for an update; otherwise False."""
    if (self._poll_interval == 0) or (datetime.now() > self._next_check_time):
      return True

  def exceeds_threshold(self, new_reading):
    """Return True if new reading is +/- the last reading by > the alert threshold.  

    Arguments:
    new_reading -- a SensorReading object

    Usage note: This should be called _before_ the update() method is called.  It would 
    normally be used to determine if update() should be called.
    """
    if (self._alert_threshold == 0):
      return False
    if (self._current_reading is None):  # special case for first pass
      return True
    if re.match(r"N\/A", str(new_reading.value())):  # special case for disconnected sensors yielding "N/A"
      return False      
    if ((new_reading.value() > (self._current_reading.value()             
                               + (self._current_reading.value() * self._alert_threshold)))     
        or (new_reading.value() < (self._current_reading.value()
                              - (self._current_reading.value() * self._alert_threshold)))):
      #print "DEBUG:  Alerting due to threshold variance!"
      return True
  
  def update(self, new_reading):
    """Takes new SensorReading for this sensor, updates sensor based on its value. Returns nothing.

    Side Effects: updates last_reading and _current_reading members, refreshes _next_check_time
    """
    self._previous_reading = self._current_reading
    self._current_reading = new_reading

    ### Important Note:  if we're polling more slowly than we're scheduled to, this time may be in the past.  That is by 
    ###    design, though the alternatives (where next_check = now + _poll_interval or where 
    ###   next_check = last_check_time + _poll_interval) are not insane.
    self._next_check_time = self._next_check_time + self._poll_interval

  def get_data_update(self):
    """Returns a SensorReading if alert critera were satisfied."""

    ## Only alert if the value has changed
    if  (self._previous_reading is None         # first time through
        or self._current_reading.value() != self._previous_reading.value()):
      return self._current_reading 
  