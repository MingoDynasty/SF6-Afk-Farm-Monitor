import json
import os
from datetime import datetime


def get_duration_since_file_modified(filename):
    modified_time = os.path.getmtime(filename)
    datetime_object = datetime.fromtimestamp(modified_time)
    duration = datetime.now() - datetime_object
    return duration


def sort_database_by_value(database_filename):
    with open(database_filename) as file:
        data = json.load(file)

    sorted_by_value = dict(sorted(data.items(), key=lambda item: item[1]))

    with open("sorted_by_value.json", "w") as file:
        json_string = json.dumps(sorted_by_value, indent=2)
        file.write(json_string)


def truncated_database(database_filename):
    with open(database_filename) as file:
        data = json.load(file)

    sorted_by_value = dict(sorted(data.items(), key=lambda item: item[1]))
    less_than_100 = {
        key: value for (key, value) in sorted_by_value.items() if value < 100
    }

    with open("shortened.json", "w") as file:
        json_string = json.dumps(less_than_100, indent=2)
        file.write(json_string)
