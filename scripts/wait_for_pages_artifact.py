#!/usr/bin/env python3
"""Wait until the freshly uploaded, attempt-specific Pages archive is discoverable."""
import json
import os
import time
import urllib.error
import urllib.request


def main():
    name = os.environ['PAGES_ARTIFACT_NAME']
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}/artifacts?per_page=100"
    deadline = time.monotonic() + 180
    while True:
        request = urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'DanceAnalyticsPages',
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                artifacts = json.load(response)['artifacts']
            found = [a for a in artifacts if a['name'] == name and not a.get('expired')]
            if len(found) > 1:
                raise RuntimeError('Several Pages archives have the current attempt name')
            if len(found) == 1:
                print(f"Pages archive ready: {name} (id {found[0]['id']})", flush=True)
                artifact_id = int(found[0]['id'])
                if os.environ.get('GITHUB_OUTPUT'):
                    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                        f.write(f'artifact_id={artifact_id}\n')
                return artifact_id
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            print(f'GitHub API returned {exc.code}; retrying', flush=True)
        if time.monotonic() >= deadline:
            raise RuntimeError('Pages archive metadata did not become available within 3 minutes')
        print(f'Waiting for Pages archive metadata: {name}', flush=True)
        time.sleep(5)

if __name__ == '__main__':
    main()
