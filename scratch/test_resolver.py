import re
import os
import sys
import json
from curl_cffi import requests as cffi_requests
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'Video_Downloader'))
from download_video import search_videos

url = "https://www.udemy.com/course/100-days-of-code/learn/lecture/19211072#content"

def resolve_udemy_lecture(url):
    print("Resolving Udemy URL:", url)
    course_slug_match = re.search(r'/course/([^/]+)', url)
    lecture_id_match = re.search(r'/lecture/(\d+)', url)
    
    if not course_slug_match:
        return None
    
    course_slug = course_slug_match.group(1)
    lecture_id = lecture_id_match.group(1) if lecture_id_match else None
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/json,*/*',
    }
    
    # Get course landing page
    course_url = f"https://www.udemy.com/course/{course_slug}/"
    r = cffi_requests.get(course_url, headers=headers, impersonate="chrome124")
    if r.status_code != 200:
        print("Course page failed:", r.status_code)
        return None
        
    course_ids = re.findall(r'courseId["\']?\s*[:=]\s*["\']?(\d+)', r.text)
    if not course_ids:
        print("Course ID not found in page")
        return None
    course_id = course_ids[0]
    
    title_match = re.findall(r'<title>(.*?)</title>', r.text)
    course_title = title_match[0].split('|')[0].strip() if title_match else course_slug
    print(f"Course ID: {course_id}, Title: {course_title}")
    
    lecture_title = None
    if lecture_id:
        curriculum_url = f"https://www.udemy.com/api-2.0/courses/{course_id}/public-curriculum-items/?page_size=1000"
        cr = cffi_requests.get(curriculum_url, headers=headers, impersonate="chrome124")
        if cr.status_code == 200:
            cdata = cr.json()
            for it in cdata.get('results', []):
                if str(it.get('id')) == str(lecture_id):
                    lecture_title = it.get('title')
                    break
                    
    print(f"Lecture ID: {lecture_id}, Title: {lecture_title}")
    
    if lecture_title:
        clean_course = re.sub(r'[^a-zA-Z0-9\s]', ' ', course_title).strip()
        # Take the primary title part
        primary_course = clean_course.split()[0:4]
        primary_course_str = " ".join(primary_course)
        query = f"{lecture_title} {primary_course_str}"
        print(f"Searching for mirrors with query: {query}")
        yt_results = search_videos(query, count=5, provider='youtube')
        if yt_results:
            top = yt_results[0]
            print(f"Top mirror match: {top.get('title')} -> {top.get('url')}")
            return {
                "course_title": course_title,
                "lecture_title": lecture_title,
                "resolved_url": top.get("url"),
                "resolved_title": top.get("title")
            }
    return None

res = resolve_udemy_lecture(url)
print("Resolution result:", res)
