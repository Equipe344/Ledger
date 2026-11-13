import { useState } from "react";
import AppShell from "../components/AppShell";
import { useAuth } from "../context/AuthContext";
import { getErrorMessage } from "../api/client";
import { handOverOffice } from "../api/auth";

const LEVELS = ["100", "200", "300", "400", "500"];

export default function Profile() {
  const { user, updateProfile, logout, isExecutive, holdsOffice } = useAuth();
  const [successorMatric, setSuccessorMatric] = useState("");
  const [handoverPassword, setHandoverPassword] = useState("");
  const [handoverError, setHandoverError] = useState("");
  const [handingOver, setHandingOver] = useState(false);

  async function handleHandover(e) {
    e.preventDefault();
    setHandoverError("");
    setHandingOver(true);
    try {
      await handOverOffice({ matric_number: successorMatric, password: handoverPassword });
      // The office is gone: sign out so the app reloads with the new role.
      await logout();
      window.location.assign("/login");
    } catch (err) {
      setHandoverError(getErrorMessage(err) || err.message);
      setHandingOver(false);
    }
  }
  const [phone, setPhone] = useState(user?.phone_number || "");
  const [level, setLevel] = useState(user?.level || "");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setSuccess(false);
    setSaving(true);
    try {
      const payload = { phone_number: phone };
      if (user?.level) payload.level = level;
      await updateProfile(payload);
      setSuccess(true);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <AppShell>
      <h1>Profile</h1>
      <p style={{ color: "var(--muted)" }}>Only your phone number and level can be changed here.</p>

      <div className="ledger-card" style={{ maxWidth: 480, padding: 24 }}>
        {success && <div className="banner success">Saved.</div>}
        {error && <div className="banner error">{error}</div>}

        <div className="field">
          <label>Matric number</label>
          <input value={user?.matric_number || ""} disabled />
        </div>
        <div className="field">
          <label>Username</label>
          <input value={user?.username || ""} disabled />
        </div>
        <div className="field">
          <label>Department</label>
          <input value={user?.department || ""} disabled />
        </div>

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label>Email (read-only)</label>
            <input value={user?.email || ""} disabled />
          </div>

          <div className="field">
            <label>Phone number</label>
            <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="080…" />
          </div>

          {user?.level && (
            <div className="field">
              <label>Level</label>
              <select value={level} onChange={(e) => setLevel(e.target.value)}>
                {LEVELS.map((l) => (
                  <option key={l} value={l}>{l}</option>
                ))}
              </select>
            </div>
          )}

          <button className="btn btn-block" type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </form>
      </div>

      {holdsOffice && (
        <div className="ledger-card" style={{ maxWidth: 480, padding: 24, marginTop: 20 }}>
          <h3 style={{ marginTop: 0 }}>Hand over your office</h3>
          <p style={{ color: "var(--muted)", marginTop: 0 }}>
            Pass the {isExecutive ? "department executive" : "class rep"} role to another student in your{" "}
            {isExecutive ? "department" : "level"}. They must have claimed their account. You become an ordinary
            student immediately and will be signed out.
          </p>
          {handoverError && <div className="banner error">{handoverError}</div>}
          <form onSubmit={handleHandover}>
            <div className="field">
              <label>Successor's matric number</label>
              <input required value={successorMatric} onChange={(e) => setSuccessorMatric(e.target.value)} />
            </div>
            <div className="field">
              <label>Your password, to confirm</label>
              <input required type="password" value={handoverPassword} onChange={(e) => setHandoverPassword(e.target.value)} />
            </div>
            <button className="btn btn-block" type="submit" disabled={handingOver}>
              {handingOver ? "Handing over…" : "Hand over"}
            </button>
          </form>
        </div>
      )}
    </AppShell>
  );
}
