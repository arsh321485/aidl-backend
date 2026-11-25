"""Let Microsoft Teams show AIDL's Teams pages (/api/teams/app/) inside a channel tab.

Every other page keeps X-Frame-Options: DENY. These pages may be framed by
Teams only (Content-Security-Policy frame-ancestors), not by any other site.
Listed after XFrameOptionsMiddleware, so it sees the response first.
"""

TEAMS_PREFIX = "/api/teams/app/"
FRAME_ANCESTORS = ("'self' https://teams.microsoft.com https://*.teams.microsoft.com https://teams.live.com "
                   "https://*.teams.live.com https://*.cloud.microsoft https://*.office.com https://*.skype.com")


class TeamsFrameMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(TEAMS_PREFIX):
            response.xframe_options_exempt = True
            response["Content-Security-Policy"] = f"frame-ancestors {FRAME_ANCESTORS}"
        return response
