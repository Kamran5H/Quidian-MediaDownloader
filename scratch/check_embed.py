import urllib.request, re

req = urllib.request.Request('https://multiembed.mov/?video_id=tt1833673', headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as r:
    html = r.read().decode('utf-8', errors='ignore')
    iframes = re.findall(r'<iframe[^>]+>', html)
    print('Iframes in multiembed:', len(iframes), iframes[:3])
    # check ad scripts
    ad_scripts = [s for s in re.findall(r'src=["\']([^"\']+)["\']', html) if any(x in s.lower() for x in ['pop', 'ad', 'click', 'track', 'tag', 'banner', 'push'])]
    print('Potential ad scripts:', len(ad_scripts), ad_scripts[:5])
