"""Maps / places / geolocation.

Two lanes (see the individual module docstrings):
  * ``geocode`` — address string -> point, cached onto the property mirror.
  * ``db.repositories.geo`` — PostGIS spatial reads over the mirror (Lane A).
External places/directions (Lane B) live in the places specialist.
"""
