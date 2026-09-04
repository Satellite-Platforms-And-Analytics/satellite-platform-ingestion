import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.resources import SatelliteResources

with SatelliteResources() as res:
    # Clear cache so new subdomains are used
    res._cache.clear()

    print('TLE Sources:')
    for s in res.tle_sources():
        print(f"  {s.get('name','')[:40]:<40} {s.get('km_subdomain','')}")

    print()
    print('Launch Providers:')
    for s in res.launch_providers():
        print(f"  {s.get('name','')[:40]:<40} {s.get('km_subdomain','')}")
