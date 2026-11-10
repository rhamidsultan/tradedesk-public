// Minimal WebAuthn client (no library): converts the server's JSON options to browser types and back.
const b2u = (b: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const u2b = (s: string) => { s = s.replace(/-/g, '+').replace(/_/g, '/'); const r = atob(s + '==='.slice((s.length + 3) % 4)); return Uint8Array.from(r, (c) => c.charCodeAt(0)).buffer }
async function post(url: string, body: any) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  if (!r.ok) { let m = `${r.status}`; try { m = (await r.json()).detail } catch { /* */ } throw new Error(m) }
  return r.json()
}
export async function registerPasskey(setup_code: string | null, name: string) {
  const o = await post('/api/auth/register/options', { setup_code })
  o.challenge = u2b(o.challenge); o.user.id = u2b(o.user.id)
  o.excludeCredentials = (o.excludeCredentials || []).map((c: any) => ({ ...c, id: u2b(c.id) }))
  const c = await navigator.credentials.create({ publicKey: o }) as PublicKeyCredential
  const r = c.response as AuthenticatorAttestationResponse
  return post('/api/auth/register/verify', { name, credential: { id: c.id, rawId: b2u(c.rawId), type: c.type,
    response: { clientDataJSON: b2u(r.clientDataJSON), attestationObject: b2u(r.attestationObject), transports: r.getTransports?.() || [] } } })
}
export async function signIn() {
  const o = await post('/api/auth/login/options', {})
  o.challenge = u2b(o.challenge)
  o.allowCredentials = (o.allowCredentials || []).map((c: any) => ({ ...c, id: u2b(c.id) }))
  const c = await navigator.credentials.get({ publicKey: o }) as PublicKeyCredential
  const r = c.response as AuthenticatorAssertionResponse
  return post('/api/auth/login/verify', { credential: { id: c.id, rawId: b2u(c.rawId), type: c.type,
    response: { clientDataJSON: b2u(r.clientDataJSON), authenticatorData: b2u(r.authenticatorData), signature: b2u(r.signature),
      userHandle: r.userHandle ? b2u(r.userHandle) : null } } })
}
