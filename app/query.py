"""Small, conservative journey phrase parser; never guesses a destination."""
import re


def interpret_journey(query: str):
    text = re.sub(r'\s+', ' ', query.strip()).strip(' ?!.')
    patterns = (
        r'how (?:do|can) i (?:get|go) from (?P<origin>.+?) to (?P<destination>.+)',
        r'(?:i(?: am|\'m)?\s+)?at (?P<origin>.+?) going to (?P<destination>.+)',
        r'how (?:i|do i) fit reach (?P<destination>.+?) from (?P<origin>.+)',
        r'(?:how (?:do|can) i (?:get|go) to )?(?P<destination>.+?) from (?P<origin>.+)',
        r'(?:from )?(?P<origin>.+?) to (?P<destination>.+)',
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        origin = match['origin'].strip(' ,?.!')
        destination = match['destination'].strip(' ,?.!')
        if 2 <= len(origin) <= 100 and 2 <= len(destination) <= 100 and origin.casefold() != destination.casefold():
            return {'origin': origin, 'destination': destination}
    return None
