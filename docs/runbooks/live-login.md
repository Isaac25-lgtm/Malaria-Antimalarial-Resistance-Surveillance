# Live eRegisters login (metadata only)

Local launcher:

```powershell
& ".\scripts\start-mars-live.ps1"
```

The live UI is `http://127.0.0.1:5173`. The live API is `http://127.0.0.1:8000`.
The database must be `mars_live`. Demo stays on separate ports and `mars_local`.

Sign in with an authorised Ministry eRegisters username and password. The
browser posts only to MARS. MARS authenticates server-to-server against
`https://eregisters.health.go.ug` over verified HTTPS and requests only:

- `GET /api/system/info`
- `GET /api/me`
- `GET /api/me/authorization`
- `GET /api/organisationUnitLevels`
- `GET /api/organisationUnitGroups`
- `GET /api/organisationUnitGroupSets`

Tracked entities, enrollments, events, relationships, patient analytics and
data value sets are not requested.

A metadata-only connectivity check (hidden password prompt, no command-line
secret):

```powershell
& ".\scripts\test-eregisters-login.ps1"
```

After a successful login, **stop**. Do not start Tracker or event
synchronisation.

Remote DHIS2 authorization and local MARS geography mapping are different
facts. A Pader data-view unit without a confirmed `geography_unit_alias`
lands on `/live/dhis2/district/{uid}` with mapping pending. It must not land
on `/no-authorised-scope`. See `docs/security/remote-authorization.md`.

Remaining work before Pader malaria indicators can appear is listed in
`docs/runbooks/pre-patient-approval.md`.
