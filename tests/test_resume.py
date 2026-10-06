"""The profile panel must work with no model configured, and must not invent
fields it cannot find."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobradar.resume import EMPTY_DETAILS, build_details, parse_details, tidy

# Deliberately mangled the way pdf extraction mangles a real CV: spaces
# scattered through the header line, words run together in the summary.
RESUME = """ALE X R MORGAN
Springfield, Ohio, United States|+1 555 0100
alex@example.com|linkedin.com/in/alex-morgan|github.com/alexmorgan
SUMMARY
Full-stack developer who shipsAI products in production. Built things.
EDUCATION
B.Tech in Computer Science Engineering, State University 2022 - 2026
SKILLS
JavaScript, Python, React
EXPERIENCE
Full-Stack Developer Intern Jun 2025 - Aug 2025
Acme Systems
AI/ML Intern Jul 2024 - Aug 2024
Globex Labs
"""

d = parse_details(RESUME)
# the header line is mangled by PDF extraction; the profile URL is not
assert d["name"] == "Alex Morgan", d["name"]
assert d["location"] == "Springfield, Ohio, United States", d["location"]
# a real job title beats a PDF-mangled prose summary
assert d["headline"] == "Full-Stack Developer Intern", d["headline"]
# ...and education is never used as the headline
assert "B.Tech" not in d["headline"]
roles = {e["years"]: e for e in d["experience"]}
assert "Jun 2025 - Aug 2025" in roles and "Jul 2024 - Aug 2024" in roles, roles
assert roles["Jun 2025 - Aug 2025"]["org"] == "Acme Systems"
assert roles["Jun 2025 - Aug 2025"]["role"] == "Full-Stack Developer Intern"
# a section heading must never be captured as an employer
assert all(e["org"].upper() not in ("SKILLS", "EXPERIENCE", "EDUCATION", "SUMMARY")
           for e in d["experience"]), d["experience"]

# nothing found -> empty, not guessed
blank = parse_details("")
assert blank == EMPTY_DETAILS, blank
one = parse_details("Jane Roe\n")
assert one["name"] == "Jane Roe" and one["experience"] == [] and one["location"] == ""

# with no experience section, fall back to the summary line
only_summary = parse_details("Ann Lee\nSUMMARY\nSenior data engineer with ten years.\n")
assert only_summary["headline"].startswith("Senior data engineer"), only_summary["headline"]

# no profile URL -> fall back to the first line rather than failing
d2 = parse_details("JAMIE LEE\nAustin, Texas, United States\nEXPERIENCE\nEngineer 2020 - 2023\nInitech\n")
assert d2["name"] == "Jamie Lee", d2["name"]
assert d2["experience"][0]["org"] == "Initech"

# a phone number line must not be mistaken for a location
d3 = parse_details("Bob Ross\n+1 555 0199, 0200\nEXPERIENCE\n")
assert d3["location"] == "", d3["location"]

# education is split out of experience, and the skills section is read
assert [e["org"] for e in d["education"]] and "B.Tech" in d["education"][0]["org"], d["education"]
assert all("B.Tech" not in e["org"] + e["role"] for e in d["experience"]), d["experience"]
assert d["skills"] == ["JavaScript", "Python", "React"], d["skills"]
assert {l["kind"]: l["url"] for l in d["links"]} == {
    "github": "https://github.com/alexmorgan",
    "linkedin": "https://www.linkedin.com/in/alex-morgan",
    "email": "mailto:alex@example.com"}, d["links"]

# school-level entries are dropped; a degree the model filed under experience
# moves to education; the same institution is not listed twice
work, study = tidy(
    [{"org": "Fabric", "role": "Backend Engineer Intern", "years": "2026 - Present"},
     {"org": "Vellore Institute of Technology", "role": "Bachelor of Technology", "years": "2023 - 2027"},
     {"org": "St. Joseph School", "role": "Class 12th (CBSE)", "years": "2020 - 2022"},
     {"org": "Galaxy Public School", "role": "Matriculation (10th Grade)", "years": "2019 - 2020"}],
    [{"org": "Vellore Institute of Technology", "role": "B.Tech CSE", "years": "2023 - 2027"}])
assert [e["org"] for e in work] == ["Fabric"], work
assert [e["org"] for e in study] == ["Vellore Institute of Technology"], study
# an internship at an institute is still work
work, study = tidy([{"org": "Indian Institute of Science", "role": "Research Intern", "years": "2024"}], [])
assert len(work) == 1 and not study

# a model reply: links come from the resume text, never from the model
class FakeLLM:
    def complete(self, prompt, max_tokens=0):
        return ('{"name": "Alex Morgan", "headline": "Developer", "location": "Ohio",'
                ' "experience": [{"org": "Acme", "role": "Intern", "years": "2025"},'
                ' {"org": "Some High School", "role": "Class 12th", "years": "2020"}],'
                ' "education": [], "skills": ["React", "", "Python"],'
                ' "links": [{"kind": "github", "url": "https://github.com/invented"}]}')
m = build_details(RESUME, FakeLLM())
assert [e["org"] for e in m["experience"]] == ["Acme"], m["experience"]
assert m["skills"] == ["React", "Python"], m["skills"]
assert all("invented" not in l["url"] for l in m["links"]) and len(m["links"]) == 3, m["links"]
print("resume parsing tests PASS")
