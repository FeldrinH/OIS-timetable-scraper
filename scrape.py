import requests
import requests_cache
import json
import csv
import signal
import datetime
import httplib2
import os
from apiclient import discovery
from google.oauth2 import service_account

lectures = {}
lecture_times = {}
available_times = {}

blacklisted_lecture_types = {'exam', 'resit', 'test', 'practice', 'e-learning'}
blacklisted_locations = {'tallinn', 'narva', 'pärnu'}

credentials = service_account.Credentials.from_service_account_file('client_secret.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
service = discovery.build('sheets', 'v4', credentials=credentials)
sheet = service.spreadsheets()

lectures_sheet_id = '1dT6zjPy2Pq8xLGfW8b3jdVgoOQZO-BLNWRd1qdOczCA' #Id of spreadsheet for available times and found lectures
lectures_range = "'Found lectures'!A2:AJ"
times_sheet_id = '1xrqm0JCq6h7Ah7FpES-BXIUhPjJ2Wz9nZx4hKCrKHRw'
times_range = "'Merili - Free time'!1:150" #"'Free times'!1:100"

session = requests_cache.CachedSession("ois_cache", allowable_methods=('GET', 'POST'))

#Enable or disable debug info printing (disabling improves performance)
debug_enabled = False
def print_debug(*args):
    if debug_enabled:
        print(*args)

def SearchPayload(start, take):
    return {"filter":{"academic_year":"2018","semester":"spring","timetable_type":"1"},"start":start,"take":take}

def GetAPI(url):
    r = session.get("https://ois2.ut.ee/api/" + url, headers={'Connection':'close'})
    r.encoding = 'UTF-8'
    r.raise_for_status()
    return r.json()

def PostAPI(url, payload):
    r = session.post("https://ois2.ut.ee/api/" + url, json=payload, headers={'Connection':'close'})
    r.encoding = 'UTF-8'
    r.raise_for_status()
    return r.json()

def TimeToFloat(time_string):
    time_parts = time_string.split(":")
    return float(time_parts[0]) + float(time_parts[1]) / 60

def IncrementDict(dictionary, key):
    if key in dictionary:
        dictionary[key] += 1
    else:
        dictionary[key] = 1

def GetAcademicWeeks(week_string):
    weeks = []
    for week_range_string in week_string.split(","):
        week_range = week_range_string.split("-")
        if len(week_range) == 1:
            weeks.append(int(week_range[0]))
        else:
            for i in range(int(week_range[0]), int(week_range[1])+1):
                weeks.append(i)
    return weeks

def ParseTimeRanges(time_string):
    time_ranges = []
    if any(char.isdigit() for char in time_string):
        for time_range_string in time_string.split(","):
            time_range = time_range_string.split("-")
            time_ranges.append((TimeToFloat(time_range[0]), TimeToFloat(time_range[1])))
    return time_ranges

def IsAllowedLocation(address_string):
    address_string_lower = address_string.lower()
    for location in blacklisted_locations:
        if location in address_string_lower:
            return False
    return True

def IsAllowedLectureType(lecture):
    lecture_type = lecture['study_work_type']['code'] if 'study_work_type' in lecture else lecture['event_type']['code']
    return lecture_type not in blacklisted_lecture_types

def IsAllowedStudyLevel(course_details):
    if 'study_levels' in course_details['additional_info']:
        for level in course_details['additional_info']['study_levels']:
            if level['code'] == "bachelor":
                return True
        return False
    else:
        return True

def GetAvailablePeople(week, day, time, duration):
    availables = set()
    if week == current_week and week in available_times and day in available_times[week]:
        for person, freetimes in available_times[week][day].items():
            for start_time, end_time in freetimes:
                if start_time <= time and time + duration <= end_time:
                    availables.add(person)
                    break
    return availables

#with open('times.csv', encoding='utf-8') as csv_file:
#    times_table = list(csv.reader(csv_file, delimiter=','))
times_table = sheet.values().get(spreadsheetId=times_sheet_id, range=times_range).execute().get('values', [])
current_week = int(times_table[1][0])
for i in range(0, len(times_table)):
    if len(times_table[i]) > 0 and times_table[i][0].startswith("Week "):
        week = int(times_table[i][0][5:])
        print("Week " + str(week))
        available_times[week] = {}
        for day in range(1,6):
            schedule_day = {}
            for person_id in range(1,len(times_table[i])):
                schedule_day[times_table[i][person_id]] = ParseTimeRanges(times_table[i+day][person_id] if len(times_table[i+day]) > person_id else "")
            available_times[week][day] = schedule_day

#print(available_times)
print("Constructed table of available times")
print("Current week:", str(current_week))

#Graceful exit setup
is_finished = False
do_cleanup = False
def ProcessKill(signal_number, frame):
    global do_cleanup
    do_cleanup = True
signal.signal(signal.SIGINT, ProcessKill)

#Main lecture data processing function
def ProcessPlans():
    global lectures, is_finished

    #Load continue data if it exists
    start_id = None
    try:
        with open('continue_data.json') as json_file:
            lectures = json.load(json_file)
            start_id = lectures.pop('[LAST]', '!!!')
            print("Loaded continue data")
            #if start_id == '!!!':
            #    print("No last plan stored, assuming processing finished")
    except FileNotFoundError:
        print("No continue data found")
    print("")

    processing_start_time = datetime.datetime.utcnow()

    chunk_size = 50
    #Load timetables in chunks of 50. Should be <500 timetables total
    for i in range(1,500,chunk_size):
        print("Block search:", i, i+chunk_size-1)
        search = PostAPI("timetable", SearchPayload(i, chunk_size))
        for offset, timetable in enumerate(search):
            print("Plan", timetable['uuid'], "\t\t" + str(i + offset))
            if start_id != None:
                if timetable['uuid'] == start_id:
                    print("Found continue location!")
                    start_id = None
                else:
                    continue
            if do_cleanup:
                lectures['[LAST]'] = timetable['uuid']
                print("Continue UUID:", timetable['uuid'])
                return

            timetable_url = '=HYPERLINK("https://ois2.ut.ee/#/timetable/' + timetable['uuid'] + '","' + timetable['info']['title']['et'] + '")'

            #Skip timetable if it has no events
            if 'course_events' not in timetable:
                print("ERROR: Empty timetable!")
                continue
            for course in timetable['course_events']:
                course_uuid = course['info']['course_uuid']
                version_uuid = course['info']['course_version_uuid']
                print_debug("Course", course_uuid, version_uuid)
                
                course_details = GetAPI("courses/" + course_uuid + "/versions/" + version_uuid)

                #Skip course if it is block mode study (sessioonõpe)
                if course_details['target']['study_type']['code'] == "openuniv":
                    continue

                #Skip course if not bachelor's. If no level specified, allow lecture by default
                if not IsAllowedStudyLevel(course_details):
                    continue

                #Get relevant info (registered count, url of timetable, human-readable course label)
                try:
                    course_info = GetAPI("registrations/courses/" + version_uuid)
                except:
                    #Skip course if unable to get registration data
                    print("ERROR: Missing registration data!")
                    continue
                registered_count = course_info['restrictions']['registered_students']
                group_count = len(course_info['groups']) if 'groups' in course_info else 0
                plan_url = '=HYPERLINK("https://ois2.ut.ee/#/timetable/course/' + course_uuid + '/' + version_uuid + '","Timetable")'
                course_url = '=HYPERLINK("https://ois2.ut.ee/#/courses/' + course_uuid + '/version/' + version_uuid + '/details","Course")'
                course_id = course_info['course']['code']
                course_name = course_details['title']['et']

                #Find lectures at suitable times for a lecture bash
                for lecture in course['events']:
                    lecture_uuid = lecture['uuid']
                    print_debug("Lecture", lecture_uuid)

                    if 'weekday' not in lecture['time'] or 'begin_time' not in lecture['time'] :
                        print("ERROR: Malformed lecture times!")
                        continue

                    #Ignore exams and practical lessons
                    if not IsAllowedLectureType(lecture):
                        continue
                    
                    day = int(lecture['time']['weekday']['code'])
                    start_time = TimeToFloat(lecture['time']['begin_time'])
                    #if IsAllowedLocation(lecture['location'].get('address', "")):
                    #    IncrementDict(lecture_times, lecture['time']['begin_time']) #Store lecture time for common lecture time statistical purposes
                    for week in GetAcademicWeeks(lecture['time']['academic_weeks']):
                        lecture_week_uuid = lecture_uuid + "_" + str(week)
                        if lecture_week_uuid not in lectures:
                            availables = GetAvailablePeople(week, day, start_time, 0) # -1/12 for 5 minutes before lecture, 0.25 for 15 minutes total (5 before start, 10 minutes during lecture)
                            if len(availables) >= 2:
                                if IsAllowedLocation(lecture['location'].get('address', "")):
                                    print("Found matching lecture", "\t\t\tWeek " + str(week), "\t" + course_id)
                                    lectures[lecture_week_uuid] = [course_details['target']['course_main_structural_unit']['code'], course_id, lecture['study_work_type']['et'], course_name, ", ".join(availables), str(registered_count), str(group_count) if group_count > 0 else "-", str(week), str(day), lecture['time']['begin_time'][:-3], course_url, plan_url, timetable_url]
                        else:
                            lectures[lecture_week_uuid].append(timetable_url)

    processing_end_time = datetime.datetime.utcnow()
    print("")
    print("Plans processed in " + str(round((processing_end_time - processing_start_time).total_seconds() / 60, 2)) + " minutes")
    is_finished = True

ProcessPlans()

with open('continue_data.json', mode='w') as json_file_out:
    json.dump(lectures, json_file_out)
    print("Written continue data to continue_data.json")

#with open('lecture_out.csv', mode='w', newline='', encoding='utf-8') as csv_file_out:
    #csv_writer = csv.writer(csv_file_out, delimiter='\t', quotechar='"', quoting=csv.QUOTE_MINIMAL)

    #if csv_file_out.tell() == 0:
    #csv_writer.writerow(["Course label", "Course name", "Available members", "Registered", "Groups", "Academic week", "Weekday", "Start time", "Course URL", "Timetable URL", "Curriculum timetable URL"])
    #for lecture_uuid in sorted(lectures):
        #if lecture_uuid != '[LAST]':
            #csv_writer.writerow(lectures[lecture_uuid])

    #print("Output generated as lecture_out.csv")

lectures.pop('[LAST]', None)
lectures_out = list(lectures.values())
sheet.values().clear(spreadsheetId=lectures_sheet_id, range=lectures_range).execute()
sheet.values().update(spreadsheetId=lectures_sheet_id, range=lectures_range, body={'values': lectures_out}, valueInputOption='USER_ENTERED').execute()

print("Output to 'UT Lectures Data' spreadsheet")

if is_finished:
    try:
        os.remove('continue_data.json')
        print("Processing finished. Removed continue data")
    except FileNotFoundError:
        print("Processing finished. No continue data stored")

#for time in sorted(lecture_times):
#    print(time + "\t" + str(lecture_times[time]))

