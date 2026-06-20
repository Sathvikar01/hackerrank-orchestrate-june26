import re
from typing import Any, Dict

from rules import normalize_object_part


_ISSUE_PATTERNS = [
    ("glass_shatter", r"\b(shatter(?:ed|ing)?|smashed glass|broken glass)\b"),
    ("crack", r"\b(crack(?:ed|ing)?|fracture|split|hairline)\b"),
    ("scratch", r"\b(scrape|scraped|scratch(?:ed)?|scuff|mark)\b"),
    ("dent", r"\b(dent(?:ed)?|ding|bump|indent)\b"),
    ("broken_part", r"\b(broken|snapped|loose|not sitting|came off|hanging|damaged)\b"),
    ("missing_part", r"\b(missing|absent|not included|empty|not there)\b"),
    ("torn_packaging", r"\b(torn|tear|ripped|opened|open seal|seal)\b"),
    ("crushed_packaging", r"\b(crushed|crumpled|smashed box|bad condition)\b"),
    ("water_damage", r"\b(water|wet|moisture|soaked|liquid)\b"),
    ("stain", r"\b(stain|residue|sticky|mark)\b"),
]

_PART_ALIASES = {
    "car": [
        ("front_bumper", r"\b(front bumper|front end|front side|bumper ke upar)\b"),
        ("rear_bumper", r"\b(rear bumper|back bumper|back of the car|back looks|bumper damage)\b"),
        ("side_mirror", r"\b(side mirror|wing mirror|mirror)\b"),
        ("windshield", r"\b(windshield|front glass)\b"),
        ("headlight", r"\b(headlight|head light)\b"),
        ("taillight", r"\b(taillight|tail light)\b"),
        ("quarter_panel", r"\b(quarter panel)\b"),
        ("fender", r"\b(fender)\b"),
        ("hood", r"\b(hood|bonnet)\b"),
        ("door", r"\b(door)\b"),
        ("body", r"\b(body|side panel|panel)\b"),
    ],
    "laptop": [
        ("trackpad", r"\b(trackpad|track pad|touchpad|touch pad)\b"),
        ("keyboard", r"\b(keyboard|keys?)\b"),
        ("screen", r"\b(screen|display|glass)\b"),
        ("hinge", r"\b(hinge)\b"),
        ("lid", r"\b(lid|cover)\b"),
        ("corner", r"\b(corner)\b"),
        ("port", r"\b(port|usb|charging)\b"),
        ("base", r"\b(base|bottom|front edge)\b"),
        ("body", r"\b(body|case|chassis)\b"),
    ],
    "package": [
        ("contents", r"\b(contents?|inside|item inside|product)\b"),
        ("item", r"\b(item|product)\b"),
        ("seal", r"\b(seal|tape|opened|tamper)\b"),
        ("label", r"\b(label|shipping label)\b"),
        ("package_corner", r"\b(corner)\b"),
        ("package_side", r"\b(side)\b"),
        ("box", r"\b(box|package|shipping box|carton)\b"),
    ],
}

_FUNCTIONAL_PATTERNS = r"\b(stopped working|does not work|doesn't work|not working|malfunction|fails?|unresponsive)\b"


def extract_claim_signals(user_claim: Any, claim_object: str) -> Dict[str, Any]:
    text = str(user_claim or "").lower()

    claimed_issue_type = "unknown"
    for issue_type, pattern in _ISSUE_PATTERNS:
        if re.search(pattern, text):
            claimed_issue_type = issue_type
            break

    claimed_object_part = "unknown"
    for part, pattern in _PART_ALIASES.get(claim_object, []):
        if re.search(pattern, text):
            claimed_object_part = normalize_object_part(part, claim_object)
            break

    return {
        "_claimed_issue_type": claimed_issue_type,
        "_claimed_object_part": claimed_object_part,
        "_claimed_functional_issue": bool(re.search(_FUNCTIONAL_PATTERNS, text)),
    }
