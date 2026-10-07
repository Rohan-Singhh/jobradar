"""The public site: every page must be indexable and well-formed, and the API
must keep nothing, cap scores like the CLI, and refuse what it should."""
import io, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SITE_URL"] = "https://jobradar.example"
os.environ["WARM_JOBS"] = "0"

from fastapi.testclient import TestClient
import jobradar.web.hosted as H
from jobradar.llm import RateLimited

c = TestClient(H.app)


def reset():
    H._jobs.update({"at": 0.0, "jobs": [], "failed": []})
    H._hits.clear()


def test_pages():
    for path in ["/", "/app", "/setup", "/author/rohan-singh"]:
        r = c.get(path)
        t = r.text
        assert r.status_code == 200, path
        assert len(re.findall(r"<h1[ >]", t)) == 1, f"{path}: exactly one h1"
        desc = re.search(r'<meta name="description" content="([^"]+)"', t).group(1)
        assert 50 <= len(desc) <= 160, f"{path}: description is {len(desc)} chars"
        canon = re.search(r'rel="canonical" href="([^"]+)"', t).group(1)
        assert canon == "https://jobradar.example" + ("" if path == "/" else path), canon
        assert "noindex" not in t, f"{path} must be indexable"
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', t, re.S):
            json.loads(block)                       # every block must be valid JSON
        for img in re.findall(r"<img [^>]+>", t):
            assert 'alt="' in img and 'width="' in img and 'height="' in img, img
    home = c.get("/").text
    assert '"FAQPage"' in home and home.count("<h3>") == len(H.FAQ), "FAQ schema must match the visible FAQ"
    assert '"BreadcrumbList"' in c.get("/setup").text
    assert '"ProfilePage"' in c.get("/author/rohan-singh").text
    assert 'class="crumbs"' in c.get("/setup").text, "breadcrumb must be visible too"
    print("page tests PASS")


def test_robots_sitemap_404():
    robots = c.get("/robots.txt").text
    assert "Allow: /" in robots and "Disallow" not in robots
    assert "Sitemap: https://jobradar.example/sitemap.xml" in robots
    sm = c.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    assert re.findall(r"<loc>(.*?)</loc>", sm.text) == [
        "https://jobradar.example", "https://jobradar.example/app",
        "https://jobradar.example/setup", "https://jobradar.example/author/rohan-singh"]
    r = c.get("/no-such-page")
    assert r.status_code == 404 and "<h1>Page not found</h1>" in r.text, "a real 404, with a page"
    assert c.get("/api/no-such").json() == {"detail": "Not Found"}
    assert c.get("/setup/", follow_redirects=False).status_code == 307, "one hop, no chain"
    print("robots/sitemap/404 tests PASS")


def test_jobs():
    reset()
    boards = {
        "Stripe": ([{"id": "s1", "company": "Stripe", "title": "Software Engineer", "url": "u1",
                     "location": "Remote", "posted_at": None, "description": "&lt;p&gt;APIs &amp;amp; more&lt;/p&gt;"},
                    {"id": "s1", "company": "Stripe", "title": "Software Engineer", "url": "u1",
                     "location": "Remote", "posted_at": None, "description": ""},
                    {"id": "s2", "company": "Stripe", "title": "Engineering Manager", "url": "u2",
                     "location": "Remote", "posted_at": None, "description": ""}], None),
    }
    H.sources.fetch_company = lambda e: boards.get(e["name"], ([], "HTTPError: 503"))
    d = c.get("/api/jobs").json()
    assert [j["id"] for j in d["jobs"]] == ["s1"], "duplicates and filtered titles must be dropped"
    assert d["jobs"][0]["desc"] == "APIs & more", "descriptions arrive as plain text"
    assert "Notion" in d["failed"]
    reset()
    H.sources.fetch_company = lambda e: ([], "down")
    assert c.get("/api/jobs").status_code == 503
    print("jobs tests PASS")


class FakeScorer:
    mode = "ok"

    def __init__(self, provider, model, key):
        assert key == "k-123"
        self.tokens_used = 42

    def score_batch(self, profile, jobs):
        if FakeScorer.mode == "rejected":
            raise RuntimeError("xkiro rejected the API key: invalid")
        if FakeScorer.mode == "limited":
            raise RateLimited("slow down")
        return {j["id"]: (9, "great fit", {"skills": 90, "domain": 90}) for j in jobs}

    def complete(self, prompt, **kw):
        if "Reply with ONLY a JSON object" in prompt:
            return '{"name": "Ann Lee", "headline": "Engineer", "experience": [], "skills": ["Go"]}'
        return "Ann Lee is a new-grad engineer."


def test_score():
    reset()
    H.ChatScorer = FakeScorer
    body = {"profile": "Ann Lee is a new-grad engineer.",
            "jobs": [{"id": "a", "company": "Acme", "title": "Software Engineer"},
                     {"id": "b", "company": "Acme", "title": "Senior Software Engineer"}]}
    key = {"X-Api-Key": "k-123"}
    r = c.post("/api/score", json=body, headers=key).json()
    assert r["results"]["a"]["score"] == 9
    assert r["results"]["b"]["score"] == 3, "the seniority cap applies here as in the CLI"
    assert r["tokens"] == 42
    assert c.post("/api/score", json=body).status_code == 400, "no key, no call"
    assert c.post("/api/score", json={**body, "provider": "ollama"}, headers=key).status_code == 400, \
        "only allow-listed providers: no reaching internal hosts"
    many = {**body, "jobs": [{"id": str(i), "company": "c", "title": "t"} for i in range(26)]}
    assert c.post("/api/score", json=many, headers=key).status_code == 422
    FakeScorer.mode = "rejected"
    r = c.post("/api/score", json=body, headers=key)
    assert r.status_code == 401 and "k-123" not in r.text, "a bad key is reported, never echoed"
    FakeScorer.mode = "limited"
    assert c.post("/api/score", json=body, headers=key).status_code == 429
    FakeScorer.mode = "ok"
    print("score tests PASS")


def test_profile():
    reset()
    H.ChatScorer = FakeScorer
    key = {"X-Api-Key": "k-123"}
    cv = ("Ann Lee\nlinkedin.com/in/ann-lee\nann@example.com\n" + "Built Go services. " * 20).encode()
    r = c.post("/api/profile", files={"resume": ("cv.txt", io.BytesIO(cv), "text/plain")}, headers=key)
    d = r.json()
    assert r.status_code == 200 and d["details"]["name"] == "Ann Lee" and d["profile"].startswith("Ann Lee")
    assert {l["kind"] for l in d["details"]["links"]} == {"linkedin", "email"}, "links come off the page"
    short = c.post("/api/profile", files={"resume": ("cv.txt", io.BytesIO(b"hi"), "text/plain")}, headers=key)
    assert short.status_code == 400
    big = c.post("/api/profile", files={"resume": ("cv.pdf", io.BytesIO(b"x" * (H.MAX_RESUME + 1)), "application/pdf")},
                 headers=key)
    assert big.status_code == 413
    print("profile tests PASS")


def test_rate_limit():
    reset()
    H.ChatScorer = FakeScorer
    key = {"X-Api-Key": "k-123"}
    n, _ = H.LIMITS["profile"]
    codes = [c.post("/api/profile", files={"resume": ("cv.txt", io.BytesIO(b"x"), "text/plain")},
                    headers=key).status_code for _ in range(n + 1)]
    assert codes[-1] == 429 and 429 not in codes[:-1], codes
    print("rate limit tests PASS")


test_pages()
test_robots_sitemap_404()
test_jobs()
test_score()
test_profile()
test_rate_limit()
