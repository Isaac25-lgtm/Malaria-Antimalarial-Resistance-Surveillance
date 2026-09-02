/**
 * The caller's own access.
 *
 * Shows exactly what the signed-in user may reach, including the permissions
 * they do *not* hold. In a system where a district officer will eventually
 * wonder why a neighbouring district is missing, making the scope legible is
 * cheaper than answering the question later.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "../../api/client";
import { useAuth } from "../../auth/context";
import { LoadingState, NoDataState } from "../../design-system/States";
import "../status/status.css";
import "./profile.css";

interface PermissionSpec {
  code: string;
  label: string;
  description: string;
  minimum_sensitivity: string;
}

export function AccessProfileView() {
  const { user } = useAuth();

  const catalogue = useQuery({
    queryKey: ["meta", "permissions"],
    queryFn: api.permissionCatalogue,
    staleTime: Infinity,
  });

  if (!user) {
    return <LoadingState label="your access" rows={3} />;
  }

  const permissions = (catalogue.data?.permissions ?? []) as PermissionSpec[];
  const held = new Set(user.permissions);

  return (
    <div className="page">
      <header className="page__header">
        <div>
          <p className="label">Your account</p>
          <h1>{user.display_name}</h1>
          <p className="page__lede">
            What this account may reach. Every restriction shown here is enforced by the
            API, not by the interface.
          </p>
        </div>
        {user.is_synthetic ? (
          <span className="chip chip--attention">Synthetic development account</span>
        ) : null}
      </header>

      <section className="panel" aria-labelledby="identity-heading">
        <div className="panel__header">
          <h2 id="identity-heading">Identity and scope</h2>
        </div>
        <div className="panel__body">
          <dl className="definition-grid">
            <div>
              <dt>Username</dt>
              <dd className="mono">{user.username}</dd>
            </div>
            <div>
              <dt>Roles</dt>
              <dd>{user.roles.join(", ") || "None"}</dd>
            </div>
            <div>
              <dt>Sensitivity ceiling</dt>
              <dd>{user.max_sensitivity.replace(/_/g, " ")}</dd>
            </div>
            <div>
              <dt>Authentication</dt>
              <dd className="mono">{user.auth_method}</dd>
            </div>
          </dl>

          <h3 className="profile__subheading">Geography scope</h3>
          {user.has_national_scope ? (
            <p>
              National. This account may read every district in Uganda at the aggregate
              level.
            </p>
          ) : user.geography_scopes.length === 0 ? (
            <NoDataState
              title="No geography scope is assigned"
              description={
                "This account cannot read any surveillance data. An empty scope is never " +
                "treated as national access - an administrator must assign a scope explicitly."
              }
              awaiting="a geography scope assignment"
            />
          ) : (
            <ul className="profile__scopes">
              {user.geography_scopes.map((scope) => (
                <li key={scope.geography_unit_id}>
                  <span className="chip chip--info">{scope.level}</span>
                  <span>{scope.name}</span>
                  <span className="mono profile__code">{scope.preferred_code}</span>
                </li>
              ))}
            </ul>
          )}

          {user.facility_scope_ids.length > 0 ? (
            <>
              <h3 className="profile__subheading">Facility scope</h3>
              <p>
                {`Restricted to ${user.facility_scope_ids.length} named facility` +
                  `${user.facility_scope_ids.length === 1 ? "" : "ies"}. ` +
                  "Sharing a district with another facility does not grant access to it."}
              </p>
            </>
          ) : null}
        </div>
      </section>

      <section className="panel" aria-labelledby="permissions-heading">
        <div className="panel__header">
          <h2 id="permissions-heading">Permissions</h2>
          <span className="chip chip--neutral">
            {`${held.size} of ${permissions.length} held`}
          </span>
        </div>
        <div className="panel__body panel__body--flush">
          {catalogue.isPending ? (
            <div className="panel__body">
              <LoadingState label="the permission catalogue" rows={5} />
            </div>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">
                  Every MARS permission and whether this account holds it
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Permission</th>
                    <th scope="col">Held</th>
                    <th scope="col">Requires</th>
                    <th scope="col">What it allows</th>
                  </tr>
                </thead>
                <tbody>
                  {permissions.map((permission) => (
                    <tr key={permission.code}>
                      <th scope="row">
                        <div>{permission.label}</div>
                        <div className="mono profile__code">{permission.code}</div>
                      </th>
                      <td>
                        {held.has(permission.code) ? (
                          <span className="chip chip--ok">Held</span>
                        ) : (
                          <span className="chip chip--neutral">Not held</span>
                        )}
                      </td>
                      <td className="mono">
                        {permission.minimum_sensitivity.replace(/_/g, " ")}
                      </td>
                      <td>{permission.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
