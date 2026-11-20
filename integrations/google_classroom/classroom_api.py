"""The Google Classroom API calls AIDL needs.

Read: a teacher's classes and their students (steps 2-3).
Write: the "AIDL" class AIDL creates at sign-up, like Slack's #aidl channel,
and its Admin Center announcement (aidl_class.py).

https://developers.google.com/classroom/reference/rest
"""

from __future__ import annotations

import logging

import requests

from .google_oauth import GoogleError

logger = logging.getLogger(__name__)

API = "https://classroom.googleapis.com/v1"
PAGE_SIZE = 100
MAX_PAGES = 50  # 5,000 items: far more than any one teacher has


def list_courses(access_token: str) -> list[dict]:
    """Active classes the signed-in person teaches."""
    return _all_pages(access_token, f"{API}/courses", "courses",
                      {"teacherId": "me", "courseStates": "ACTIVE"})


def list_students(access_token: str, course_id: str) -> list[dict]:
    return _all_pages(access_token, f"{API}/courses/{course_id}/students", "students", {})


def list_teachers(access_token: str, course_id: str) -> list[dict]:
    return _all_pages(access_token, f"{API}/courses/{course_id}/teachers", "teachers", {})


def get_course(access_token: str, course_id: str) -> dict:
    return _call(access_token, "GET", f"{API}/courses/{course_id}")


def create_course(access_token: str, body: dict) -> dict:
    return _call(access_token, "POST", f"{API}/courses", body=body)


def create_announcement(access_token: str, course_id: str, text: str, student_ids: list[str] | None = None,
                        drive_file_id: str = "") -> dict:
    """student_ids: only those students (and the teachers) see it. drive_file_id: a picture attached."""
    body = {"text": text, "state": "PUBLISHED"}
    if drive_file_id:
        body["materials"] = [{"driveFile": {"driveFile": {"id": drive_file_id}, "shareMode": "VIEW"}}]
    if student_ids:
        body.update(assigneeMode="INDIVIDUAL_STUDENTS", individualStudentsOptions={"studentIds": student_ids})
    return _call(access_token, "POST", f"{API}/courses/{course_id}/announcements", body=body)


def delete_announcement(access_token: str, course_id: str, announcement_id: str) -> dict:
    return _call(access_token, "DELETE", f"{API}/courses/{course_id}/announcements/{announcement_id}")


DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"


def upload_png(access_token: str, name: str, png: bytes) -> str:
    """A PNG into the teacher's Google Drive (drive.file). Returns the file id."""
    import json as _json

    boundary = "aidl-licence-boundary"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps({'name': name, 'mimeType': 'image/png'})}\r\n"
            f"--{boundary}\r\nContent-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--".encode()
    try:
        resp = requests.request("POST", DRIVE_UPLOAD, params={"uploadType": "multipart"}, data=body, timeout=30,
                                headers={"Authorization": f"Bearer {access_token}",
                                         "Content-Type": f"multipart/related; boundary={boundary}"})
    except Exception as exc:  # noqa: BLE001
        raise GoogleError("drive_unreachable", "Could not reach Google Drive.") from exc
    if not resp.ok:
        logger.warning("drive upload %s: %s", resp.status_code, resp.text[:200])
        raise GoogleError("drive_forbidden" if resp.status_code in (401, 403) else "drive_error",
                          "Google Drive didn't accept the licence picture.")
    return resp.json().get("id", "")


def share_file(access_token: str, file_id: str, email: str) -> None:
    """Let one person view the file (no email from Google)."""
    _call(access_token, "POST", f"{DRIVE_FILES}/{file_id}/permissions",
          params={"sendNotificationEmail": "false"}, body={"role": "reader", "type": "user", "emailAddress": email})


def remove_student(access_token: str, course_id: str, user_id: str) -> dict:
    return _call(access_token, "DELETE", f"{API}/courses/{course_id}/students/{user_id}")


def create_coursework(access_token: str, course_id: str, body: dict) -> dict:
    return _call(access_token, "POST", f"{API}/courses/{course_id}/courseWork", body=body)


def modify_assignees(access_token: str, course_id: str, coursework_id: str, *, add: list[str] = (),
                     remove: list[str] = ()) -> dict:
    """Who sees a question (individual students)."""
    options = {}
    if add:
        options["addStudentIds"] = list(add)
    if remove:
        options["removeStudentIds"] = list(remove)
    return _call(access_token, "POST", f"{API}/courses/{course_id}/courseWork/{coursework_id}:modifyAssignees",
                 body={"assigneeMode": "INDIVIDUAL_STUDENTS", "modifyIndividualStudentsOptions": options})


def update_coursework(access_token: str, course_id: str, coursework_id: str, **fields) -> dict:
    return _call(access_token, "PATCH", f"{API}/courses/{course_id}/courseWork/{coursework_id}",
                 body=fields, params={"updateMask": ",".join(fields)})


def delete_coursework(access_token: str, course_id: str, coursework_id: str) -> dict:
    return _call(access_token, "DELETE", f"{API}/courses/{course_id}/courseWork/{coursework_id}")


def list_announcements(access_token: str, course_id: str) -> list[dict]:
    return _all_pages(access_token, f"{API}/courses/{course_id}/announcements", "announcements", {})


def list_materials(access_token: str, course_id: str) -> list[dict]:
    """The class's Classwork materials, drafts included."""
    return _all_pages(access_token, f"{API}/courses/{course_id}/courseWorkMaterials", "courseWorkMaterial",
                      {"courseWorkMaterialStates": ["PUBLISHED", "DRAFT"]})


def list_coursework(access_token: str, course_id: str) -> list[dict]:
    """The class's live questions / assignments (Google leaves out deleted ones)."""
    return _all_pages(access_token, f"{API}/courses/{course_id}/courseWork", "courseWork", {})


def list_submissions(access_token: str, course_id: str, coursework_id: str) -> list[dict]:
    return _all_pages(access_token, f"{API}/courses/{course_id}/courseWork/{coursework_id}/studentSubmissions",
                      "studentSubmissions", {})


def update_announcement(access_token: str, course_id: str, announcement_id: str, text: str) -> dict:
    return _call(access_token, "PATCH", f"{API}/courses/{course_id}/announcements/{announcement_id}",
                 body={"text": text}, params={"updateMask": "text"})


def create_draft_material(access_token: str, course_id: str, title: str, description: str,
                          link: str = "", topic_id: str = "") -> dict:
    """A Classwork material in DRAFT state: only the class's teachers see it."""
    body = {"title": title, "description": description, "state": "DRAFT"}
    if topic_id:
        body["topicId"] = topic_id
    if link:
        body["materials"] = [{"link": {"url": link}}]
    return _call(access_token, "POST", f"{API}/courses/{course_id}/courseWorkMaterials", body=body)


def create_class_material(access_token: str, course_id: str, title: str, description: str,
                          topic_id: str = "") -> dict:
    """A published Classwork material for every student of the class."""
    body = {"title": title, "description": description, "state": "PUBLISHED"}
    if topic_id:
        body["topicId"] = topic_id
    return _call(access_token, "POST", f"{API}/courses/{course_id}/courseWorkMaterials", body=body)


def create_student_material(access_token: str, course_id: str, title: str, description: str, *, link: str = "",
                            drive_file_id: str = "", student_ids: list[str], topic_id: str = "") -> dict:
    """A published Classwork material that only these students (and the teachers) see.
    link: a web page attached. drive_file_id: a picture attached."""
    body = {"title": title, "description": description, "state": "PUBLISHED",
            "assigneeMode": "INDIVIDUAL_STUDENTS", "individualStudentsOptions": {"studentIds": student_ids}}
    if link:
        body["materials"] = [{"link": {"url": link}}]
    elif drive_file_id:
        body["materials"] = [{"driveFile": {"driveFile": {"id": drive_file_id}, "shareMode": "VIEW"}}]
    if topic_id:
        body["topicId"] = topic_id
    try:
        return _call(access_token, "POST", f"{API}/courses/{course_id}/courseWorkMaterials", body=body)
    except GoogleError as exc:
        if exc.code == "classroom_forbidden" or not link:
            raise
        # Classroom may refuse a link it can't load (e.g. http://localhost): put it in the text instead.
        body.pop("materials")
        body["description"] = f"{description}\n{link}"
        return _call(access_token, "POST", f"{API}/courses/{course_id}/courseWorkMaterials", body=body)


def delete_material(access_token: str, course_id: str, material_id: str) -> dict:
    return _call(access_token, "DELETE", f"{API}/courses/{course_id}/courseWorkMaterials/{material_id}")


def update_material(access_token: str, course_id: str, material_id: str, **fields) -> dict:
    """fields: title, topicId, ..."""
    return _call(access_token, "PATCH", f"{API}/courses/{course_id}/courseWorkMaterials/{material_id}",
                 body=fields, params={"updateMask": ",".join(fields)})


def list_invitations(access_token: str, course_id: str) -> list[dict]:
    """Pending invitations to the class (classroom.rosters)."""
    return _all_pages(access_token, f"{API}/invitations", "invitations", {"courseId": course_id})


def create_invitation(access_token: str, course_id: str, user_id: str, role: str = "STUDENT") -> dict:
    """Invite someone to the class (classroom.rosters). user_id: Google id or email."""
    return _call(access_token, "POST", f"{API}/invitations", body={"courseId": course_id, "userId": user_id,
                                                                   "role": role})


def delete_invitation(access_token: str, invitation_id: str) -> dict:
    return _call(access_token, "DELETE", f"{API}/invitations/{invitation_id}")


def get_user_profile(access_token: str, user_id: str) -> dict:
    return _get(access_token, f"{API}/userProfiles/{user_id}", {})


def list_topics(access_token: str, course_id: str) -> list[dict]:
    return _all_pages(access_token, f"{API}/courses/{course_id}/topics", "topic", {})


def delete_topic(access_token: str, course_id: str, topic_id: str) -> dict:
    return _call(access_token, "DELETE", f"{API}/courses/{course_id}/topics/{topic_id}")


def create_topic(access_token: str, course_id: str, name: str) -> dict:
    return _call(access_token, "POST", f"{API}/courses/{course_id}/topics", body={"name": name})


def _all_pages(access_token: str, url: str, key: str, params: dict) -> list[dict]:
    items, token = [], ""
    for _ in range(MAX_PAGES):
        query = {**params, "pageSize": PAGE_SIZE}
        if token:
            query["pageToken"] = token
        data = _get(access_token, url, query)
        items.extend(data.get(key) or [])
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return items


def _get(access_token: str, url: str, params: dict) -> dict:
    return _call(access_token, "GET", url, params=params)


def _call(access_token: str, method: str, url: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        if method == "GET":  # requests.get: the tests patch it
            resp = requests.get(url, params=params, headers=headers, timeout=20)
        else:
            resp = requests.request(method, url, params=params, json=body, headers=headers, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("classroom api unreachable: %s", exc)
        raise GoogleError("classroom_unreachable", "Could not reach Google Classroom. Please try again.") from exc
    if resp.ok:
        return resp.json()

    try:
        error = resp.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or ""
    logger.warning("classroom api %s %s: %s", resp.status_code, error.get("status"), message)
    if resp.status_code == 409:
        raise GoogleError("already_exists", "That's already there in Google Classroom.")
    if resp.status_code == 404:
        raise GoogleError("not_found", "That Google Classroom class or post no longer exists.")
    if resp.status_code == 401:
        raise GoogleError("reconnect", "Google Classroom access has expired. Please connect again.")
    if resp.status_code == 403 and ("has not been used" in message or "is disabled" in message
                                    or "SERVICE_DISABLED" in str(error)):
        raise GoogleError("classroom_api_disabled",
                          "The Google Classroom API is switched off for AIDL's Google Cloud project. "
                          "Turn it on in Google Cloud → APIs & Services → Library → Google Classroom API.")
    if resp.status_code == 403:
        raise GoogleError("classroom_forbidden",
                          "Google Classroom didn't allow AIDL to read your classes. If your school manages "
                          "your Google account, your Workspace admin may need to allow AIDL.")
    raise GoogleError("classroom_error", "Google Classroom returned an error. Please try again.")
