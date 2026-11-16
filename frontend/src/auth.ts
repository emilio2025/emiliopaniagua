// Inicio de sesión con Google (Google Identity Services).
// El ID token solo se guarda en memoria/sessionStorage de esta pestaña y se envía
// al backend propio como Authorization: Bearer. El backend verifica la firma.
import { fetchAuthConfig, fetchMe, setAuthToken } from './api';
import { el } from './dom';

interface GoogleCredentialResponse {
  credential?: string;
}

interface GoogleIdApi {
  initialize(options: {
    client_id: string;
    callback: (response: GoogleCredentialResponse) => void;
    auto_select?: boolean;
    cancel_on_tap_outside?: boolean;
    ux_mode?: 'popup' | 'redirect';
  }): void;
  renderButton(parent: HTMLElement, options: Record<string, string | number>): void;
  disableAutoSelect(): void;
}

declare global {
  interface Window {
    google?: { accounts: { id: GoogleIdApi } };
  }
}

export interface AuthUser {
  name: string;
  email: string;
}

const STORAGE_KEY = 'flash.google.idtoken';
const GSI_SRC = 'https://accounts.google.com/gsi/client';

let enabled = false;
let user: AuthUser | null = null;
let slot: HTMLElement | null = null;
let onChange: (() => void) | null = null;
let gsiReady: Promise<GoogleIdApi> | null = null;

export function isAuthEnabled(): boolean {
  return enabled;
}

export function currentUser(): AuthUser | null {
  return user;
}

function tokenExpiry(token: string): number {
  try {
    const payload = token.split('.')[1] ?? '';
    const json = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/'))) as { exp?: unknown };
    return typeof json.exp === 'number' ? json.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

function storeToken(token: string | null): void {
  try {
    if (token) {
      window.sessionStorage.setItem(STORAGE_KEY, token);
    } else {
      window.sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* almacenamiento no disponible: la sesión dura lo que la página */
  }
}

function readStoredToken(): string | null {
  try {
    const token = window.sessionStorage.getItem(STORAGE_KEY);
    return token && tokenExpiry(token) > Date.now() + 30_000 ? token : null;
  } catch {
    return null;
  }
}

function loadGsi(clientId: string): Promise<GoogleIdApi> {
  if (gsiReady) {
    return gsiReady;
  }
  gsiReady = new Promise<GoogleIdApi>((resolve, reject) => {
    const done = (): void => {
      const api = window.google?.accounts.id;
      if (!api) {
        reject(new Error('Google Identity Services no disponible'));
        return;
      }
      api.initialize({
        client_id: clientId,
        callback: (response) => {
          void handleCredential(response.credential ?? '');
        },
        auto_select: false,
        cancel_on_tap_outside: true,
        ux_mode: 'popup'
      });
      resolve(api);
    };
    if (window.google?.accounts.id) {
      done();
      return;
    }
    const script = document.createElement('script');
    script.src = GSI_SRC;
    script.async = true;
    script.defer = true;
    script.onload = done;
    script.onerror = () => reject(new Error('No se pudo cargar Google'));
    document.head.append(script);
  });
  return gsiReady;
}

async function handleCredential(token: string): Promise<void> {
  if (!token) {
    return;
  }
  setAuthToken(token);
  try {
    user = await fetchMe();
    storeToken(token);
  } catch {
    user = null;
    setAuthToken(null);
    storeToken(null);
  }
  renderSlot();
  onChange?.();
}

export function signOut(): void {
  user = null;
  setAuthToken(null);
  storeToken(null);
  window.google?.accounts.id.disableAutoSelect();
  renderSlot();
  onChange?.();
}

export function renderSignInButton(container: HTMLElement): void {
  if (!gsiReady) {
    return;
  }
  void gsiReady
    .then((api) => {
      container.replaceChildren();
      api.renderButton(container, {
        type: 'standard',
        theme: 'outline',
        size: 'large',
        text: 'signin_with',
        shape: 'pill',
        locale: 'es'
      });
    })
    .catch(() => {
      container.replaceChildren(el('p', { class: 'auth-error' }, 'No se pudo cargar el inicio de sesión de Google.'));
    });
}

function renderSlot(): void {
  if (!slot) {
    return;
  }
  slot.replaceChildren();
  if (!enabled) {
    return;
  }
  if (user) {
    const logout = el('button', { type: 'button', class: 'nav-link auth-logout' }, 'Salir');
    logout.addEventListener('click', signOut);
    slot.append(el('span', { class: 'auth-user', title: user.email }, `Hola, ${user.name}`), logout);
    return;
  }
  const holder = el('div', { class: 'auth-google' });
  slot.append(holder);
  renderSignInButton(holder);
}

export async function initAuth(target: HTMLElement, changed: () => void): Promise<void> {
  slot = target;
  onChange = changed;
  try {
    const config = await fetchAuthConfig();
    enabled = config.google_enabled && Boolean(config.google_client_id);
    if (!enabled || !config.google_client_id) {
      renderSlot();
      return;
    }
    void loadGsi(config.google_client_id).catch(() => undefined);
    const stored = readStoredToken();
    if (stored) {
      setAuthToken(stored);
      try {
        user = await fetchMe();
      } catch {
        user = null;
        setAuthToken(null);
        storeToken(null);
      }
    }
  } catch {
    enabled = false;
  }
  renderSlot();
  onChange?.();
}
