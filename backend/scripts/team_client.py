"""Small HTTP client for Persons 1–3. Keep the service token on their servers."""
import httpx


class BookPoolClient:
    def __init__(self, base_url, service_key):
        self.http = httpx.Client(base_url=base_url,
                                 headers={"Authorization": "Bearer " + service_key}, timeout=30)

    def call(self, method, path, body=None):
        response = self.http.request(method, "/v1" + path, json=body)
        response.raise_for_status()
        return response.json()

    def create_user(self, telegram_id, display_name):
        return self.call("POST", "/users", {"telegram_id": str(telegram_id), "display_name": display_name})

    def submit_request(self, request):
        return self.call("POST", "/requests", request)

    def save_offer(self, offer):
        return self.call("POST", "/offers", offer)

    def propose_group(self, proposal):
        return self.call("POST", "/groups", proposal)

    def approve(self, group_id, user_id, version):
        return self.call("POST", f"/groups/{group_id}/participants/{user_id}/approve", {"version": version})

    def authorize_mock(self, group_id, user_id, version):
        return self.call("POST", f"/groups/{group_id}/participants/{user_id}/authorize", {"version": version})

    def execute(self, group_id, version):
        return self.call("POST", f"/groups/{group_id}/execute", {"version": version})

    def close(self):
        self.http.close()
