from typing import List, Dict

CLAIM_OBJECTS = ["car", "laptop", "package"]

CLAIM_STATUS = ["supported", "contradicted", "not_enough_information"]

ISSUE_TYPES = [
    "dent",
    "scratch",
    "crack",
    "glass_shatter",
    "broken_part",
    "missing_part",
    "torn_packaging",
    "crushed_packaging",
    "water_damage",
    "stain",
    "none",
    "unknown",
]

OBJECT_PARTS: Dict[str, List[str]] = {
    "car": [
        "front_bumper",
        "rear_bumper",
        "door",
        "hood",
        "windshield",
        "side_mirror",
        "headlight",
        "taillight",
        "fender",
        "quarter_panel",
        "body",
        "unknown",
    ],
    "laptop": [
        "screen",
        "keyboard",
        "trackpad",
        "hinge",
        "lid",
        "corner",
        "port",
        "base",
        "body",
        "unknown",
    ],
    "package": [
        "box",
        "package_corner",
        "package_side",
        "seal",
        "label",
        "contents",
        "item",
        "unknown",
    ],
}

RISK_FLAGS = [
    "none",
    "blurry_image",
    "cropped_or_obstructed",
    "low_light_or_glare",
    "wrong_angle",
    "wrong_object",
    "wrong_object_part",
    "damage_not_visible",
    "claim_mismatch",
    "possible_manipulation",
    "non_original_image",
    "text_instruction_present",
    "user_history_risk",
    "manual_review_required",
]

SEVERITY = ["none", "low", "medium", "high", "unknown"]

OUTPUT_COLUMNS = [
    "user_id",
    "image_paths",
    "user_claim",
    "claim_object",
    "evidence_standard_met",
    "evidence_standard_met_reason",
    "risk_flags",
    "issue_type",
    "object_part",
    "claim_status",
    "claim_status_justification",
    "supporting_image_ids",
    "valid_image",
    "severity",
]

EVIDENCE_REQUIREMENTS_BY_OBJECT = {
    "car": [
        "REQ_CAR_BODY_PANEL",
        "REQ_CAR_GLASS_LIGHT_MIRROR",
        "REQ_CAR_IDENTITY_OR_SIDE",
    ],
    "laptop": [
        "REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD",
        "REQ_LAPTOP_BODY_HINGE_PORT",
    ],
    "package": [
        "REQ_PACKAGE_EXTERIOR",
        "REQ_PACKAGE_LABEL_OR_STAIN",
        "REQ_PACKAGE_CONTENTS",
    ],
    "all": [
        "REQ_GENERAL_OBJECT_PART",
        "REQ_GENERAL_MULTI_IMAGE",
        "REQ_REVIEW_TRUST",
    ],
}

SEVERITY_ORDER = {"none": 0, "unknown": 1, "low": 2, "medium": 3, "high": 4}
