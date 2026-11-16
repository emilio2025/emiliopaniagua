// Inicio de sesión con Google (Google Identity Services).
// El ID token solo se guarda en memoria/sessionStorage de esta pestaña y se envía
// al backend propio como Authorization: Bearer. El backend verifica la firma.
import { ApiError, fetchAuthConfig, fetchMe, loginWithPassword, registerAccount, setAuthToken } from './api';
import { el, icon } from './dom';

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
let googleEnabled = false;
let passwordEnabled = false;
let formMode: 'login' | 'register' = 'login';
let user: AuthUser | null = null;
let slot: HTMLElement | null = null;
let onChange: (() => void) | null = null;
let gsiReady: Promise<GoogleIdApi> | null = null;
let loginDialog: HTMLDialogElement | null = null;

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
    loginDialog?.close();
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

function field(label: string, input: HTMLInputElement): HTMLElement {
  return el('label', { class: 'auth-field' }, el('span', {}, label), input);
}

function authErrorMessage(error: unknown, mode: 'login' | 'register'): string {
  if (error instanceof ApiError) {
    if (error.code === 'INVALID_CREDENTIALS') {
      return 'Correo o contraseña incorrectos.';
    }
    if (error.code === 'EMAIL_TAKEN') {
      return 'Ese correo ya tiene una cuenta. Inicia sesión.';
    }
    if (error.code === 'VALIDATION_ERROR') {
      return mode === 'register'
        ? 'Revisa los datos: nombre, un correo válido y una contraseña de 8 caracteres o más.'
        : 'Escribe un correo y una contraseña válidos.';
    }
    if (error.code === 'RATE_LIMITED') {
      return 'Demasiados intentos. Espera un minuto e inténtalo de nuevo.';
    }
  }
  return 'No pudimos conectar con el servidor. Inténtalo de nuevo.';
}

async function completeSession(token: string, sessionUser: AuthUser): Promise<void> {
  setAuthToken(token);
  user = sessionUser;
  storeToken(token);
  loginDialog?.close();
  renderSlot();
  onChange?.();
}

function buildPasswordForm(): HTMLElement {
  const name = el('input', { type: 'text', name: 'name', autocomplete: 'name', maxlength: '80', placeholder: 'Tu nombre' }) as HTMLInputElement;
  const email = el('input', { type: 'email', name: 'email', autocomplete: 'email', required: '', maxlength: '254', placeholder: 'tucorreo@ejemplo.com' }) as HTMLInputElement;
  const password = el('input', { type: 'password', name: 'password', required: '', maxlength: '128', placeholder: '••••••••' }) as HTMLInputElement;
  const nameField = field('Nombre', name);
  const submit = el('button', { type: 'submit', class: 'button button-primary auth-submit' }, 'Ingresar') as HTMLButtonElement;
  const error = el('p', { class: 'auth-form-error', role: 'alert' });
  error.hidden = true;
  const switchText = el('span', {});
  const switchButton = el('button', { type: 'button', class: 'auth-switch-button' }) as HTMLButtonElement;
  const form = el(
    'form',
    { class: 'auth-form', novalidate: '' },
    nameField,
    field('Correo electrónico', email),
    field('Contraseña', password),
    error,
    submit,
    el('p', { class: 'auth-switch' }, switchText, switchButton)
  ) as HTMLFormElement;

  const applyMode = (): void => {
    const registering = formMode === 'register';
    nameField.hidden = !registering;
    name.required = registering;
    password.autocomplete = registering ? 'new-password' : 'current-password';
    password.minLength = registering ? 8 : 1;
    submit.textContent = registering ? 'Crear cuenta' : 'Ingresar';
    switchText.textContent = registering ? '¿Ya tienes cuenta? ' : '¿No tienes cuenta? ';
    switchButton.textContent = registering ? 'Inicia sesión' : 'Crear cuenta';
    const title = loginDialog?.querySelector('.auth-dialog-title');
    if (title) {
      title.textContent = registering ? 'Crear cuenta' : 'Iniciar sesión';
    }
    error.hidden = true;
  };

  switchButton.addEventListener('click', () => {
    formMode = formMode === 'login' ? 'register' : 'login';
    applyMode();
    (formMode === 'register' ? name : email).focus();
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const mode = formMode;
    const emailValue = email.value.trim();
    const nameValue = name.value.trim();
    if (!emailValue || !password.value || (mode === 'register' && nameValue.length < 2)) {
      error.textContent = mode === 'register' ? 'Completa nombre, correo y contraseña.' : 'Escribe tu correo y contraseña.';
      error.hidden = false;
      return;
    }
    if (mode === 'register' && password.value.length < 8) {
      error.textContent = 'La contraseña debe tener al menos 8 caracteres.';
      error.hidden = false;
      return;
    }
    submit.disabled = true;
    error.hidden = true;
    const action =
      mode === 'register'
        ? registerAccount(nameValue, emailValue, password.value)
        : loginWithPassword(emailValue, password.value);
    void action
      .then(async (result) => {
        password.value = '';
        await completeSession(result.token, result.user);
      })
      .catch((reason: unknown) => {
        error.textContent = authErrorMessage(reason, mode);
        error.hidden = false;
        password.select();
      })
      .finally(() => {
        submit.disabled = false;
      });
  });

  form.dataset.ready = '1';
  (form as HTMLFormElement & { applyMode?: () => void }).applyMode = applyMode;
  return form;
}

function ensureLoginDialog(): HTMLDialogElement {
  if (loginDialog) {
    return loginDialog;
  }
  const close = el('button', { type: 'button', class: 'icon-button auth-dialog-close', 'aria-label': 'Cerrar' }, icon('close'));
  const card = el(
    'div',
    { class: 'auth-dialog-card' },
    el(
      'div',
      { class: 'auth-dialog-head' },
      el('h2', { id: 'auth-dialog-title', class: 'auth-dialog-title' }, 'Iniciar sesión'),
      close
    ),
    el('p', { class: 'auth-dialog-text' }, 'Accede a FLASH para comprar y ver tu nombre en el comprobante.')
  );
  if (passwordEnabled) {
    card.append(buildPasswordForm());
  }
  if (googleEnabled) {
    if (passwordEnabled) {
      card.append(el('div', { class: 'auth-divider' }, el('span', {}, 'o')));
    }
    card.append(el('div', { class: 'auth-dialog-google' }));
  }
  card.append(el('p', { class: 'auth-dialog-note' }, 'Tu contraseña se guarda cifrada. Con Google solo usamos tu nombre y correo.'));
  const dialog = el('dialog', { class: 'auth-dialog', 'aria-labelledby': 'auth-dialog-title' }, card) as HTMLDialogElement;
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) {
      dialog.close();
    }
  });
  document.body.append(dialog);
  loginDialog = dialog;
  return dialog;
}

export function openLogin(): void {
  if (!enabled || user) {
    return;
  }
  const dialog = ensureLoginDialog();
  formMode = 'login';
  const form = dialog.querySelector('.auth-form') as (HTMLFormElement & { applyMode?: () => void }) | null;
  form?.applyMode?.();
  const holder = dialog.querySelector<HTMLElement>('.auth-dialog-google');
  if (holder && gsiReady) {
    void gsiReady
      .then((api) => {
        holder.replaceChildren();
        api.renderButton(holder, {
          type: 'standard',
          theme: 'outline',
          size: 'large',
          text: 'continue_with',
          shape: 'rectangular',
          logo_alignment: 'left',
          width: 320,
          locale: 'es'
        });
      })
      .catch(() => {
        holder.replaceChildren(el('p', { class: 'auth-error' }, 'No se pudo cargar el inicio de sesión de Google.'));
      });
  }
  if (!dialog.open) {
    dialog.showModal();
    window.setTimeout(() => dialog.querySelector<HTMLInputElement>('input[name=email]')?.focus(), 50);
  }
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
  const login = el('button', { type: 'button', class: 'login-button', 'aria-haspopup': 'dialog' }, 'Iniciar sesión');
  login.addEventListener('click', openLogin);
  slot.append(login);
}

export async function initAuth(target: HTMLElement, changed: () => void): Promise<void> {
  slot = target;
  onChange = changed;
  try {
    const config = await fetchAuthConfig();
    googleEnabled = config.google_enabled && Boolean(config.google_client_id);
    passwordEnabled = config.password_enabled;
    enabled = googleEnabled || passwordEnabled;
    if (googleEnabled && config.google_client_id) {
      void loadGsi(config.google_client_id).catch(() => undefined);
    }
    const stored = enabled ? readStoredToken() : null;
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
