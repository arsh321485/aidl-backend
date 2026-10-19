"""Split E:\\highway-traffic-cards.html into one page per card, ready for a
2x headless-Chrome screenshot: no section label, no watermark, transparent
page, and a script that writes the card + logo-circle positions into the
page title (read back with --dump-dom)."""
import re
import sys

src = open(sys.argv[1], encoding="utf-8").read()
head = src[: src.index("<body")]
body = src[src.index("<body"):]
extra = """<style>
html, body { background: transparent !important; margin: 0 !important; padding: 0 !important; }
.section-label, .watermark { display: none !important; }
.card-section { margin: 0 !important; padding: 0 !important; }
.msg { margin: 0 !important; box-shadow: none !important; width: 600px; max-width: 600px !important; }
</style>
<script>
window.addEventListener('load', () => {
  const msg = document.querySelector('.msg').getBoundingClientRect();
  const av = document.querySelector('.avatar').getBoundingClientRect();
  document.title = JSON.stringify({w: msg.width, h: msg.height,
    avatar: [av.left - msg.left, av.top - msg.top, av.width, av.height]});
});
</script>
"""
for card_id, out in (("card-highway", "hc"), ("card-traffic", "tl")):
    m = re.search(r'<div class="card-section" id="%s">.*?(?=<!-- =|</body>)' % card_id, body, flags=re.S)
    page = head.replace("</head>", extra + "</head>") + "<body>" + m.group(0) + "</body></html>"
    open(f"{sys.argv[2]}/{out}.html", "w", encoding="utf-8").write(page)
print("ok")
