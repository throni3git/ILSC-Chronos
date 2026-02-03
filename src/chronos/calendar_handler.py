# python lib
from hashlib import md5
from pathlib import Path
from urllib.request import urlretrieve
import datetime as dt
import logging
import time
import zoneinfo

# external libs
import caldav
import icalendar
import x_wr_timezone

# own code
from chronos.config import Config
from chronos.chronos_event import ChronosEvent


logger = logging.getLogger(__name__)


class CalendarHandler:
    def __init__(self, app_config: Config):
        self.app_config = app_config

        app_timezone = zoneinfo.ZoneInfo(self.app_config.get("app", "timezone"))
        self.last_check = (dt.datetime.now() - dt.timedelta(days=7)).astimezone(app_timezone)
        self.cal_timezone_info = zoneinfo.ZoneInfo("UTC")

        self.events_data: dict[str, ChronosEvent] = {}

        self.client = None
        self.calendar = None
        self.principal = None

        # derived from calendars.json
        self.cal_primary = None
        self.cal_name = None
        self.cal_user = None
        self.cal_passwd = None

        self.force_time = False  # affects only 24h allday events
        self.force_start = None
        self.force_end = None

        self.ignore_planned = False
        self.ignore_descriptions = False
        self.title_prefix = None
        self.tags = []
        self.color = None
        self.default_location = None

        self.tags_excluded = []

        self.sanitize = {"stati": True, "source_icons": True, "target_icons": True}

        self.icons = {}

    @property
    def chronos_id(self) -> str:
        result = md5(f"{self.cal_name}_{self.cal_primary}".encode("utf-8")).hexdigest()
        return result

    @property
    def sanitize_stati(self) -> bool:
        return self.sanitize["stati"]

    @property
    def sanitize_icons_src(self) -> bool:
        return self.sanitize["source_icons"]

    @property
    def sanitize_icons_tgt(self) -> bool:
        return self.sanitize["target_icons"]

    def config(self, conf_data):
        for key, val in conf_data.items():
            if type(val) is dict:
                # setattr(self, key, getattr(self, key) | val)
                setattr(self, key, {**getattr(self, key), **val})
            else:
                setattr(self, key, val)

    def read(self) -> None:
        """read calendar events. decides if it is from a ICS file or from a CalDAV calendar."""

        if ".ics" in self.cal_primary or "?export" in self.cal_primary:
            self.read_ics_from_url()
        else:
            self.read_from_cal_dav()

    def read_ics_from_url(self):
        """read events from .ics file from the calendars primary adress"""

        # reset data first
        self.events_data = {}

        # can't open ICS file directly, so first download
        pathname_tmp = Path("./tmp")
        pathname_tmp.mkdir(parents=True, exist_ok=True)
        fn_cal = pathname_tmp / f"tmp_{self.cal_name}.ics"
        urlretrieve(self.cal_primary, fn_cal)

        # TODO 2025-04-21 handle ICS file not being accessible

        with fn_cal.open(encoding="utf-8") as f:
            calendar_contents = f.read()
            ics_calendar = icalendar.Calendar.from_ical(calendar_contents)

        # safety check for correct calendar
        if str(ics_calendar["X-WR-CALNAME"]) != self.cal_name:
            logger.error(f"mismatch of calendar name ({ics_calendar['X-WR-CALNAME']=} vs {self.cal_name=})")
            return

        # use standardized format for timezone and find timezone
        standardized_icalendar = x_wr_timezone.to_standard(ics_calendar, add_timezone_component=True)
        timezone_id: str | None = None
        for subcomp in standardized_icalendar.walk("VTIMEZONE"):
            if timezone_id is not None:
                logger.error(f"multiple timezone instances in calendar '{self.cal_name}': '{timezone_id}' and '{subcomp.tz_name}'")
            timezone_id = subcomp.tz_name
        self.cal_timezone_info = zoneinfo.ZoneInfo(timezone_id)

        # compare with the time zone of the target calendar
        timezone_from_config = self.app_config.get("app", "timezone")
        target_timezone = zoneinfo.ZoneInfo(timezone_from_config)
        if target_timezone != self.cal_timezone_info:
            logger.warning(f"timezone of calendar ({self.cal_timezone_info}) is not the same as the target calendars timezone ({target_timezone})")

        for event in ics_calendar.walk("VEVENT"):
            # event_summary = str(event.get("SUMMARY"))
            # print(f"{event_summary=}")

            new_chronos_event = ChronosEvent(self)
            new_chronos_event._ics_event = event.copy()

            # Only handle public events and those not containing exclude tags
            if not new_chronos_event.is_confidential and not new_chronos_event.is_excluded and not new_chronos_event.date_out_of_range:
                new_chronos_event.populate_from_vcal_object()

                # TODO 2025-06-11 put into helper function for date range check
                # determine limits of time range with timezone info
                today_in_the_morning_utc = dt.datetime.today().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=zoneinfo.ZoneInfo("UTC"))
                range_min = self.app_config.get("calendars", "range_min")
                limit_start_date = today_in_the_morning_utc + dt.timedelta(days=range_min)
                range_max = self.app_config.get("calendars", "range_max")
                limit_end_date = today_in_the_morning_utc + dt.timedelta(days=range_max)

                # handle different input types (dt.date or dt.datetime) with timezone info
                if isinstance(new_chronos_event.dt_start, dt.datetime):
                    dt_start = new_chronos_event.dt_start
                else:
                    dt_start = dt.datetime(
                        year=new_chronos_event.dt_start.year,
                        month=new_chronos_event.dt_start.month,
                        day=new_chronos_event.dt_start.day,
                        tzinfo=zoneinfo.ZoneInfo("UTC"),
                    )

                if isinstance(new_chronos_event.dt_end, dt.datetime):
                    dt_end = new_chronos_event.dt_end
                else:
                    dt_end = dt.datetime(
                        year=new_chronos_event.dt_end.year,
                        month=new_chronos_event.dt_end.month,
                        day=new_chronos_event.dt_end.day,
                        tzinfo=zoneinfo.ZoneInfo("UTC"),
                    )

                # check for limits
                if dt_start < limit_start_date:
                    # print("event is in the past")
                    continue

                if dt_end > limit_end_date:
                    # print("event is in the far future")
                    continue

                self.events_data[new_chronos_event.key] = new_chronos_event

    def read_from_cal_dav(self) -> None:
        """read events from caldav calendar"""
        logger.debug(f'Connecting Calendar "{self.cal_name}"')

        start = time.time()
        try:
            self.client = caldav.DAVClient(self.cal_primary, username=self.cal_user, password=self.cal_passwd)
            self.principal = self.client.principal()
        except Exception as ex:
            logger.critical(f"Error on CALDav auth: {ex}")
            raise

        self.events_data = {}

        logger.debug("Time needed: {:.2f}s".format(time.time() - start))
        start = time.time()

        logger.debug("Reading Events")
        list_available_calendars = self.available_calendars()
        for calendar in list_available_calendars:
            if calendar and calendar.name == self.cal_name:
                self.calendar = calendar
                # TODO: Check if timezone or utc converion is needed
                # had to add 2 hours else duplicates are created
                today_in_the_morning_utc = dt.datetime.today().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=zoneinfo.ZoneInfo("UTC"))
                range_min = self.app_config.get("calendars", "range_min")
                limit_start_date = today_in_the_morning_utc + dt.timedelta(days=range_min)
                range_max = self.app_config.get("calendars", "range_max")
                limit_end_date = today_in_the_morning_utc + dt.timedelta(days=range_max)

                logger.debug(f'Checking calendar "{self.cal_name}" for dates in range: {limit_start_date} to {limit_end_date}')

                try:
                    upcoming_events = calendar.search(
                        start=limit_start_date,
                        end=limit_end_date,
                        event=True,
                        expand=True,
                    )
                except Exception:
                    # print("Your calendar server does apparently not support expanded search")
                    upcoming_events = calendar.search(
                        start=limit_start_date,
                        end=limit_end_date,
                        event=True,
                        expand=False,
                    )

                # get all events
                # events = calendar.events()
                for event in upcoming_events:
                    if event.data:
                        try:
                            self.read_event(event)
                        except Exception as ex:
                            logger.error(f"Error reading event: {ex}")
        # uncomment as helper to check fetched events sorted by sektion and dates
        # dates = [value.key for (key, value) in sorted(self.events_data.items(), reverse=False)]
        logger.debug("Time needed: {:.2f}s".format(time.time() - start))

        if self.calendar is None:
            raise ValueError(f"read_from_cal_dav: target calendar '{self.cal_name}' was not found!")

    def available_calendars(self) -> list[caldav.Calendar]:
        cals = self.principal.calendars()
        logger.info(f"Fetching available calendars on: {self.cal_name}")
        logger.debug("Found:")
        for cal in cals:
            logger.debug(f"\t{cal.name}")
        return cals

    def read_event(self, calEvent: caldav.Event) -> None:
        """read event data"""
        # TODO: Clean this mess. As there should only be one vevent component. at least if caldav filter is working
        cal = icalendar.Calendar.from_ical(calEvent.data)
        components = cal.walk("vevent")
        # logger.debug(f'Nr of vevent components {len(components)}')
        for component in components:
            if component.name == "VEVENT":
                """
                #only needed if parsing all events in calendar
                #TODO: check what this was for ^^
                edate = component.get('dtstart').dt
                if isinstance(edate, datetime):
                    edate = edate.date()
                #if edate >= date.start_date():
                """
                chronos_event = ChronosEvent(self)
                chronos_event.calDAV = calEvent

                # Only handle public events and those not conataining exclude tags
                if not chronos_event.is_confidential and not chronos_event.is_excluded and not chronos_event.date_out_of_range:
                    chronos_event.populate_from_vcal_object()
                    self.events_data[chronos_event.key] = chronos_event

    def search_events_by_tags(self, tags: list) -> dict:
        """search read events created by chronos with given tags
        #TODO: Check newer caldav version for direct search
        """
        found = {}
        for key, event in self.events_data.items():
            if set(tags).issubset(event.categories) and event.is_chronos_origin:
                found[key] = event
        return found

    def search_events_by_calid(self, calid: str) -> dict[str, ChronosEvent]:
        """search read events created by chronos with given calendar id"""
        found = {}
        for key, event in self.events_data.items():
            if calid == event.cal_id and event.is_chronos_origin:
                found[key] = event
        return found

    def close_connection(self) -> None:
        if self.client is not None:
            self.client.close()
