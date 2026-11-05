"""One kit per forge for the contract suite (test_contract.py): each turns the same normalized
scenario into that forge's own response shapes, served by conftest.World, and builds the adapter
over it. A scenario is written once; every forge's adapter must read it back the same way."""

from __future__ import annotations

from conftest import TOKEN, World, response
from forge_tracker.forges import forgejo, github, gitlab
from forge_tracker.transport import ReadTransport

# A conflict scenario in each forge's words. "settling" is the moment right after the target
# branch moves, when the forge hasn't re-checked yet.
GITHUB_MERGEABLE = {"yes": "CONFLICTING", "no": "MERGEABLE", "unknown": "UNKNOWN", "settling": None}
GITLAB_STATUS = {
    "yes": ("conflict", True),
    "no": ("mergeable", False),
    "unknown": ("checking", False),
    # GitLab says unchecked, with has_conflicts still false, until it re-checks.
    "settling": ("unchecked", False),
}
GITLAB_PIPELINE = {"success": "success", "failure": "failed", None: None}


def _rest_issue_common(spec):
    return {
        "title": spec.get("title", f"issue {spec['number']}"),
        "created_at": spec.get("created", "2026-10-11T00:00:00Z"),
        "updated_at": spec.get("updated", "2026-10-12T00:00:00Z"),
        "closed_at": spec.get("closed"),
        "body": spec.get("body", ""),
    }


class GitHubKit:
    name = "github"
    change_sigil = "#"
    secret = TOKEN  # the token the transport sends; an error that echoes it must hide it

    def world(self):
        return World()

    def adapter(self, world, page_cap=50):
        t = ReadTransport(
            api_base="https://api.github.com",
            http=world,
            token=TOKEN,
            graphql_url="https://api.github.com/graphql",
            page_cap=page_cap,
        )
        return github.GitHub(t, "acme/widgets")

    def user(self, world, login, bot):
        world.route("/user", {"login": login, "type": "Bot" if bot == "type" else "User"})

    def _issue(self, spec):
        raw = _rest_issue_common(spec)
        milestone = spec.get("milestone")
        raw.update(
            number=spec["number"],
            state=spec.get("state", "open"),
            labels=[{"name": n} for n in spec.get("labels", ())],
            assignees=[{"login": n} for n in spec.get("assignees", ())],
            milestone={"title": milestone[0], "due_on": milestone[1]} if milestone else None,
            user={"login": spec.get("author", "someone"), "type": "User"},
            html_url=f"https://github.com/acme/widgets/issues/{spec['number']}",
        )
        return raw

    def issues(self, world, specs):
        world.route("/repos/acme/widgets/issues", [self._issue(s) for s in specs])

    def failing_issues(self, world, status, message):
        world.route("/repos/acme/widgets/issues", {"message": message}, status=status)

    def endless_issues(self, world):
        def endless(url):
            return response(
                [self._issue({"number": 1})],
                headers={
                    "Link": '<https://api.github.com/repos/acme/widgets/issues?page=2>; rel="next"'
                },
            )

        world.route("/repos/acme/widgets/issues", endless)

    def _pr(self, spec):
        pipeline = spec.get("pipeline")
        return {
            "number": spec["number"],
            "title": f"change {spec['number']}",
            "url": f"https://github.com/acme/widgets/pull/{spec['number']}",
            "headRefName": spec.get("branch", f"feat/{spec['number']}"),
            "isDraft": spec.get("draft", False),
            "mergeable": GITHUB_MERGEABLE[spec.get("conflicts", "no")],
            "createdAt": "2026-10-11T00:00:00Z",
            "updatedAt": spec.get("updated", "2026-10-12T00:00:00Z"),
            "mergedAt": spec.get("merged"),
            "author": {"__typename": "User", "login": spec.get("author", "me")},
            "mergeCommit": {"oid": spec["sha"]} if spec.get("sha") else None,
            "assignees": {"nodes": [{"login": n} for n in spec.get("assignees", ())]},
            "reviewRequests": {
                "nodes": [
                    {"requestedReviewer": {"__typename": "User", "login": n}}
                    for n in spec.get("reviewers", ())
                ]
            },
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": "abc",
                            "statusCheckRollup": {"state": pipeline.upper()} if pipeline else None,
                        }
                    }
                ]
            },
            "closingIssuesReferences": {
                "nodes": [
                    {"number": n, "repository": {"nameWithOwner": "acme/widgets"}}
                    for n in spec.get("closes", ())
                ]
            },
        }

    def _connection(self, nodes, key="pullRequests"):
        return {
            "repository": {
                key: {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
            }
        }

    def changes(self, world, specs):
        world.op("OpenChanges", self._connection([self._pr(s) for s in specs]))

    def merged(self, world, specs):
        world.op("MergedChanges", self._connection([self._pr(s) for s in specs]))

    def links(self, world, links):
        by_issue = {}
        for blocked, blocker, is_open, project in links:
            by_issue.setdefault(blocked, []).append(
                {
                    "number": blocker,
                    "state": "OPEN" if is_open else "CLOSED",
                    "repository": {"nameWithOwner": project or "acme/widgets"},
                }
            )
        nodes = [{"number": n, "blockedBy": {"nodes": b}} for n, b in by_issue.items()]
        world.op("BlockingLinks", self._connection(nodes, key="issues"))

    def run(self, world, conclusion, jobs):
        world.route(
            "/repos/acme/widgets/actions/runs",
            {
                "workflow_runs": [
                    {
                        "id": 100,
                        "name": "CI",
                        "event": "push",
                        "status": "completed",
                        "conclusion": conclusion,
                        "run_attempt": 1,
                        "created_at": "2026-10-12T00:00:00Z",
                    }
                ]
                if conclusion
                else []
            },
        )
        world.route(
            "/repos/acme/widgets/actions/runs/100/jobs",
            {"jobs": [{"name": n, "conclusion": c, "run_attempt": 1} for n, c in jobs]},
        )

    def releases(self, world, releases, tags):
        world.route(
            "/repos/acme/widgets/releases",
            [{"tag_name": t, "published_at": d, "draft": False} for t, d in releases],
        )
        world.route("/repos/acme/widgets/tags", [{"name": t} for t in tags])

    # --- /groom: possibly done

    patches = True
    can_miss_files = False

    def cross_refs(self, world, mentions):
        """`mentions`: open issue -> [(change, state, merged_at)], plus a mention from another
        issue, which isn't a change."""
        nodes = []
        for issue, changes in mentions.items():
            items = [{"willCloseTarget": False, "source": {"__typename": "Issue", "number": 99}}]
            for change, state, merged in changes:
                items.append(
                    {
                        "willCloseTarget": False,
                        "source": {
                            "__typename": "PullRequest",
                            "number": change,
                            "title": f"change {change}",
                            "state": state.upper(),
                            "mergedAt": merged,
                            "baseRefName": "main",
                            "repository": {"nameWithOwner": "acme/widgets"},
                        },
                    }
                )
            nodes.append(
                {"number": issue, "timelineItems": {"totalCount": len(items), "nodes": items}}
            )
        world.op("CrossRefs", self._connection(nodes, key="issues"))

    def files(self, world, number, files):
        world.route(
            f"/repos/acme/widgets/pulls/{number}/files",
            [
                dict(
                    {"filename": path, "status": "renamed" if old else "modified"},
                    **({"patch": patch} if patch else {}),
                    **({"previous_filename": old} if old else {}),
                )
                for path, patch, old in files
            ],
        )


class GitLabKit:
    name = "gitlab"
    change_sigil = "!"
    secret = "glpat-AbCdEfGhIjKlMnOpQrSt"  # a token shape, hidden by pattern
    P = "/api/v4/projects/acme%2Fwidgets"

    def world(self):
        return World().route(self.P, {"id": 42, "default_branch": "main"})

    def adapter(self, world, page_cap=50):
        t = ReadTransport(api_base="https://gitlab.com/api/v4", http=world, page_cap=page_cap)
        return gitlab.GitLab(t, "acme/widgets")

    def user(self, world, login, bot):
        world.route(
            "/api/v4/user",
            {"username": login, "bot": bot == "type"},
        )

    def _issue(self, spec):
        raw = _rest_issue_common(spec)
        milestone = spec.get("milestone")
        due = milestone[1][:10] if milestone and milestone[1] else None
        raw.update(
            iid=spec["number"],
            project_id=42,
            state="opened" if spec.get("state", "open") == "open" else "closed",
            labels=list(spec.get("labels", ())),
            assignees=[{"username": n} for n in spec.get("assignees", ())],
            milestone={"title": milestone[0], "due_date": due} if milestone else None,
            author={"username": spec.get("author", "someone")},
            web_url=f"https://gitlab.com/acme/widgets/-/issues/{spec['number']}",
            description=raw.pop("body"),
        )
        return raw

    def issues(self, world, specs):
        world.route(f"{self.P}/issues", [self._issue(s) for s in specs])

    def failing_issues(self, world, status, message):
        world.route(f"{self.P}/issues", {"message": message}, status=status)

    def endless_issues(self, world):
        def endless(url):
            return response([self._issue({"number": 1})], headers={"X-Next-Page": "2"})

        world.route(f"{self.P}/issues", endless)

    def _mr(self, spec):
        detailed, has_conflicts = GITLAB_STATUS[spec.get("conflicts", "no")]
        pipeline = GITLAB_PIPELINE[spec.get("pipeline")]
        return {
            "iid": spec["number"],
            "project_id": 42,
            "title": f"change {spec['number']}",
            "source_branch": spec.get("branch", f"feat/{spec['number']}"),
            "target_branch": "main",
            "draft": spec.get("draft", False),
            "author": {"username": spec.get("author", "me")},
            "assignees": [{"username": n} for n in spec.get("assignees", ())],
            "reviewers": [{"username": n} for n in spec.get("reviewers", ())],
            "created_at": "2026-10-11T00:00:00Z",
            "updated_at": spec.get("updated", "2026-10-12T00:00:00Z"),
            "merged_at": spec.get("merged"),
            "merge_commit_sha": spec.get("sha"),
            "detailed_merge_status": detailed,
            "has_conflicts": has_conflicts,
            "head_pipeline": {"status": pipeline} if pipeline else None,
            "web_url": f"https://gitlab.com/acme/widgets/-/merge_requests/{spec['number']}",
        }

    def changes(self, world, specs):
        raws = [self._mr(s) for s in specs]
        world.route(f"{self.P}/merge_requests", raws)
        for raw in raws:
            world.route(f"{self.P}/merge_requests/{raw['iid']}", raw)
            world.route(f"{self.P}/merge_requests/{raw['iid']}/approvals", {})

    def merged(self, world, specs):
        world.route(f"{self.P}/merge_requests", [self._mr(s) for s in specs])
        for s in specs:
            world.route(
                f"{self.P}/merge_requests/{s['number']}/closes_issues",
                [{"iid": n, "project_id": 42} for n in s.get("closes", ())],
            )

    def links(self, world, links):
        blocked = sorted({b for b, *_ in links})
        world.route(f"{self.P}/issues", [self._issue({"number": n}) for n in blocked])
        for n in blocked:
            world.route(
                f"{self.P}/issues/{n}/links",
                [
                    {
                        "iid": blocker,
                        "project_id": 42 if project is None else 77,
                        "references": {"full": f"{project or 'acme/widgets'}#{blocker}"},
                        "state": "opened" if is_open else "closed",
                        "link_type": "is_blocked_by",
                    }
                    for b, blocker, is_open, project in links
                    if b == n
                ],
            )

    def run(self, world, conclusion, jobs):
        status = {"success": "success", "failure": "failed"}.get(conclusion)
        world.route(
            f"{self.P}/pipelines",
            [{"id": 100, "status": status, "source": "push", "created_at": "2026-10-12T00:00:00Z"}]
            if conclusion
            else [],
        )
        world.route(
            f"{self.P}/pipelines/100/jobs",
            [
                {"id": i, "name": n, "status": "failed" if c == "failure" else c}
                for i, (n, c) in enumerate(jobs, 1)
            ],
        )

    def releases(self, world, releases, tags):
        world.route(
            f"{self.P}/releases",
            [{"tag_name": t, "released_at": d} for t, d in releases],
        )
        world.route(f"{self.P}/repository/tags", [{"name": t} for t in tags])

    # --- /groom: possibly done

    patches = True
    can_miss_files = True

    def cross_refs(self, world, mentions):
        world.route(f"{self.P}/issues", [self._issue({"number": n}) for n in mentions])
        for issue, changes in mentions.items():
            world.route(
                f"{self.P}/issues/{issue}/related_merge_requests",
                [
                    dict(
                        self._mr({"number": change, "merged": merged}),
                        state="opened" if state == "open" else state,
                        references={"full": f"acme/widgets!{change}"},
                    )
                    for change, state, merged in changes
                ],
            )

    def files(self, world, number, files):
        world.route(
            f"{self.P}/merge_requests/{number}/diffs",
            [
                {
                    "old_path": old or path,
                    "new_path": path,
                    "renamed_file": bool(old),
                    "diff": patch or "",
                }
                for path, patch, old in files
            ],
        )

    def missing_files(self, world, number):
        world.route(f"{self.P}/merge_requests/{number}/diffs", {"message": "404 Not found"}, 404)


FORGEJO_TOKEN = "f0rgejo5ecret0token1234567890abcdef"
FORGEJO_MERGEABLE = {"yes": False, "no": True, "unknown": None, "settling": None}
FORGEJO_STATUS = {"success": "success", "failure": "failure", None: None}


class ForgejoKit:
    name = "forgejo"
    change_sigil = "#"
    secret = FORGEJO_TOKEN  # the token in use, hidden wherever an error echoes it
    R = "/api/v1/repos/acme/widgets"

    def world(self):
        return World()

    def adapter(self, world, page_cap=50):
        t = ReadTransport(
            api_base="https://code.example.org/api/v1",
            http=world,
            token=FORGEJO_TOKEN,
            page_cap=page_cap,
            auth_scheme=forgejo.AUTH_SCHEME,
            page_size_param="limit",
            page_size=forgejo.PAGE_SIZE,
        )
        return forgejo.Forgejo(t, "acme/widgets")

    def user(self, world, login, bot):
        world.route("/api/v1/user", {"login": f"{login}[bot]" if bot == "type" else login})

    def _issue(self, spec):
        raw = _rest_issue_common(spec)
        milestone = spec.get("milestone")
        raw.update(
            number=spec["number"],
            id=1000 + spec["number"],
            state=spec.get("state", "open"),
            labels=[{"name": n} for n in spec.get("labels", ())],
            assignees=[{"login": n} for n in spec.get("assignees", ())] or None,
            milestone={"title": milestone[0], "due_on": milestone[1] + "T00:00:00Z"}
            if milestone
            else None,
            user={"login": spec.get("author", "someone")},
            html_url=f"https://code.example.org/acme/widgets/issues/{spec['number']}",
            pull_request=None,
        )
        return raw

    def issues(self, world, specs):
        world.route(f"{self.R}/issues", [self._issue(s) for s in specs])

    def failing_issues(self, world, status, message):
        world.route(f"{self.R}/issues", {"message": message}, status=status)

    def endless_issues(self, world):
        def endless(url):
            return response(
                [self._issue({"number": 1})],
                headers={
                    "Link": f'<https://code.example.org{self.R}/issues?page=2&limit=50>; rel="next"'
                },
            )

        world.route(f"{self.R}/issues", endless)

    def _pr(self, spec):
        n = spec["number"]
        return {
            "number": n,
            "id": 2000 + n,
            "title": f"change {n}",
            "body": " ".join(f"Closes #{i}" for i in spec.get("closes", ())),
            "state": "closed" if spec.get("merged") else "open",
            "draft": spec.get("draft", False),
            "mergeable": FORGEJO_MERGEABLE[spec.get("conflicts", "no")],
            "merged": bool(spec.get("merged")),
            "merged_at": spec.get("merged"),
            "merge_commit_sha": spec.get("sha"),
            "user": {"login": spec.get("author", "me")},
            "assignees": [{"login": a} for a in spec.get("assignees", ())],
            "requested_reviewers": [{"login": r} for r in spec.get("reviewers", ())],
            "head": {"ref": spec.get("branch", f"feat/{n}"), "sha": f"head{n}"},
            "base": {"ref": "main"},
            "created_at": "2026-10-11T00:00:00Z",
            "updated_at": spec.get("updated", "2026-10-12T00:00:00Z"),
            "html_url": f"https://code.example.org/acme/widgets/pulls/{n}",
        }

    def changes(self, world, specs):
        world.route(f"{self.R}/pulls", [self._pr(s) for s in specs])
        for s in specs:
            state = FORGEJO_STATUS[s.get("pipeline")]
            world.route(
                f"{self.R}/commits/head{s['number']}/status",
                {"state": state or "", "total_count": 1 if state else 0, "statuses": []},
            )
            world.route(f"{self.R}/pulls/{s['number']}/reviews", [])

    def merged(self, world, specs):
        world.route(f"{self.R}/pulls", [self._pr(s) for s in specs])

    def links(self, world, links):
        blocked = sorted({b for b, *_ in links})
        world.route(f"{self.R}/issues", [self._issue({"number": n}) for n in blocked])
        for n in blocked:
            world.route(
                f"{self.R}/issues/{n}/dependencies",
                [
                    dict(
                        self._issue({"number": blocker, "state": "open" if is_open else "closed"}),
                        repository={"full_name": project or "acme/widgets"},
                    )
                    for b, blocker, is_open, project in links
                    if b == n
                ],
            )

    def run(self, world, conclusion, jobs):
        world.route(
            f"{self.R}/actions/runs",
            {
                "workflow_runs": [
                    {
                        "id": 100,
                        "display_title": "CI",
                        "event": "push",
                        "head_branch": "main",
                        "status": "completed",
                        "conclusion": conclusion,
                        "run_attempt": 1,
                        "created_at": "2026-10-12T00:00:00Z",
                    }
                ]
                if conclusion
                else [],
                "total_count": 1 if conclusion else 0,
            },
        )
        world.route(
            f"{self.R}/actions/runs/100/jobs",
            {"jobs": [{"name": n, "conclusion": c, "run_attempt": 1} for n, c in jobs]},
        )

    def releases(self, world, releases, tags):
        world.route(
            f"{self.R}/releases",
            [{"tag_name": t, "published_at": d, "draft": False} for t, d in releases],
        )
        world.route(f"{self.R}/tags", [{"name": t} for t in tags])

    # --- /groom: possibly done

    patches = False  # Forgejo's changed-file list has names only
    can_miss_files = True

    def cross_refs(self, world, mentions):
        world.route(f"{self.R}/issues", [self._issue({"number": n}) for n in mentions])
        for issue, changes in mentions.items():
            events = [
                {"type": "comment", "body": "a plain comment"},
                # An issue (not a PR) naming this one: not a change.
                {"type": "issue_ref", "ref_issue": dict(self._issue({"number": 99}))},
            ]
            for change, state, merged in changes:
                events.append(
                    {
                        "type": "pull_ref",
                        "ref_issue": dict(
                            self._issue({"number": change, "title": f"change {change}"}),
                            state="closed" if state == "merged" else state,
                            repository={"full_name": "acme/widgets"},
                            pull_request={"merged": state == "merged", "merged_at": merged},
                        ),
                    }
                )
            world.route(f"{self.R}/issues/{issue}/timeline", events)

    def files(self, world, number, files):
        world.route(
            f"{self.R}/pulls/{number}/files",
            [
                dict({"filename": path}, **({"previous_filename": old} if old else {}))
                for path, _patch, old in files
            ],
        )

    def missing_files(self, world, number):
        world.route(self.R, {"default_branch": "main"})
        world.route(f"{self.R}/pulls/{number}/files", {"message": "not found"}, 404)


KITS = [GitHubKit(), GitLabKit(), ForgejoKit()]
