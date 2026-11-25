#!/usr/bin/env python3
"""Deploy the exact artifact discovered by our REST probe, without a second lookup."""
import json
import os
import time
import urllib.error
import urllib.request


def api(method, route, payload=None):
    req = urllib.request.Request('https://api.github.com' + route, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'User-Agent': 'DanceAnalyticsPages', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    artifact_id = int(os.environ['PAGES_ARTIFACT_ID'])
    if not isinstance(artifact_id, int) or artifact_id <= 0:
        raise RuntimeError('Invalid artifact ID')
    request = urllib.request.Request(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'],
        headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urllib.request.urlopen(request, timeout=30) as response:
        identity = json.load(response)['value']
    route = '/repos/' + os.environ['GITHUB_REPOSITORY'] + '/pages/deployments'
    # The same official endpoint and payload used by actions/deploy-pages.
    deployment = api('POST', route, {'artifact_id': artifact_id,
        'pages_build_version': os.environ['GITHUB_SHA'], 'oidc_token': identity})
    deployment_id = deployment.get('id') or deployment.get('status_url', '').rstrip('/').rsplit('/', 1)[-1] or os.environ['GITHUB_SHA']
    print(f'Pages deployment created for artifact {artifact_id}', flush=True)
    deadline = time.monotonic() + 600
    failures = {'deployment_failed','deployment_perms_error','deployment_content_failed','deployment_cancelled','deployment_lost'}
    while time.monotonic() < deadline:
        try:
            result = api('GET', route + '/' + str(deployment_id))
        except urllib.error.HTTPError as exc:
            if exc.code not in {404,429,500,502,503,504}:
                raise
            time.sleep(5)
            continue
        status = result.get('status')
        print('Pages status:', status, flush=True)
        if status == 'succeed':
            print('Site published:', deployment.get('page_url','https://julia-briukva.github.io/Dance-analytics/'))
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
                    f.write('\nGitHub Pages: publication completed successfully.\n')
            return
        if status in failures:
            raise RuntimeError('GitHub Pages returned ' + status)
        time.sleep(5)
    raise RuntimeError('Timed out waiting for GitHub Pages publication; check deployment status')

if __name__ == '__main__':
    main()
