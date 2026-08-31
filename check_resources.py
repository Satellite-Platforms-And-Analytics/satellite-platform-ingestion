import os
import sys

# Resolve `src.resources` from this repo, not from a sibling project.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.resources import SatelliteResources

with SatelliteResources() as res:
    print('TLE Sources:')
    for s in res.tle_sources():
        name = s.get('name','')[:35]
        url  = s.get('url','')[:55]
        print(f'  {name:<35} {url}')

    print()
    print('Sample tracking databases:')
    for s in res.tracking_databases()[:8]:
        name = s.get('name','')[:35]
        url  = s.get('url','')[:55]
        print(f'  {name:<35} {url}')

    print()
    print('Sample launch providers:')
    for s in res.launch_providers()[:8]:
        name = s.get('name','')[:35]
        url  = s.get('url','')[:55]
        print(f'  {name:<35} {url}')
