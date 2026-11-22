# Bundled geo datasets

## radon_zones.csv (county_fips → EPA radon zone 1/2/3)

EPA Map of Radon Zones (public domain, EPA 402-R-93-071), assembled
2026-08-17 from two independent public mirrors and cross-validated at
99.2% agreement:

- Primary: EPA county radon table joined to 2022 Census county shapes
  (public-domain EPA data via a public GitHub mirror).
- Gap-fill (145 counties — LA parishes, VA independent cities, AK):
  ZIP-level EPA zones from the ZipCheckup dataset (CC BY 4.0,
  https://github.com/zipcheckup — attribution required by that license),
  aggregated to county FIPS via the bgruber/zip2fips crosswalk (MIT),
  clear-majority only.

Not covered (loader returns None → "no data yet"): Puerto Rico (EPA
doesn't zone it), Connecticut's 2022 planning-region GEOIDs, and 10
tied-majority counties. Zone meanings: 1 = high (>4 pCi/L predicted),
2 = moderate, 3 = low.
