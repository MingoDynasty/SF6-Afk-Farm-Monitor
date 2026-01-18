import os
from datetime import datetime


def get_duration_since_file_modified(filename):
    modified_time = os.path.getmtime(filename)
    datetime_object = datetime.fromtimestamp(modified_time)
    duration = datetime.now() - datetime_object
    return duration
