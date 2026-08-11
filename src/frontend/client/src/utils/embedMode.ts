const truthyValues = new Set(['1', 'true', 'yes', 'on']);
const falsyValues = new Set(['0', 'false', 'no', 'off']);
const storageKey = 'bisheng_embed_mode';

function setStoredEmbeddedMode(enabled: boolean): void {
  try {
    if (enabled) {
      window.sessionStorage.setItem(storageKey, '1');
    } else {
      window.sessionStorage.removeItem(storageKey);
    }
  } catch {
    // Storage can be unavailable in some embedded/browser privacy modes.
  }
}

function getStoredEmbeddedMode(): boolean {
  try {
    return window.sessionStorage.getItem(storageKey) === '1';
  } catch {
    return false;
  }
}

export function isEmbeddedMode(): boolean {
  if (typeof window === 'undefined') {
    return false;
  }

  const params = new URLSearchParams(window.location.search);
  const explicitValue = params.get('embed') ?? params.get('embedded') ?? params.get('iframe');
  if (explicitValue) {
    const normalized = explicitValue.toLowerCase();
    if (truthyValues.has(normalized)) {
      setStoredEmbeddedMode(true);
      return true;
    }
    if (falsyValues.has(normalized)) {
      setStoredEmbeddedMode(false);
      return false;
    }
  }

  if (getStoredEmbeddedMode()) {
    return true;
  }

  try {
    return window.self !== window.top;
  } catch {
    return true;
  }
}
