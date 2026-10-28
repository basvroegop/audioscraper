import json
import time
import urllib.request

url='http://127.0.0.1:8788/api/episodes/260f47a34cb6454aaa02a130b2ed1866'
previous=None
for _ in range(120):
    with urllib.request.urlopen(url,timeout=10) as response:
        item=json.load(response)
    if item['stage']!=previous:
        print(item['stage'],flush=True)
        previous=item['stage']
    if item['status']=='completed':
        print(json.dumps({'title':item['title'],'words':len(item['transcript'].split()),'segments':len(item['segments']),'entities':len(item['entities'])}),flush=True)
        break
    if item['status']=='failed':
        raise RuntimeError(item['error'])
    time.sleep(5)
else:
    raise TimeoutError('Test is still processing')
