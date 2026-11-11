// The login wall: the only page the instance serves without a session.
// A right password sets the session cookie, then the UI loads in its place.

const form = document.querySelector('#login');
const error = form.querySelector('[data-error]');
const submit = form.querySelector('[data-submit]');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.textContent = '';
  submit.disabled = true;
  try {
    const response = await fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: form.password.value }),
    });
    if (response.ok) {
      window.location.replace('/');
      return;
    }
    if (response.status === 401) {
      error.textContent = 'Wrong password.';
    } else if (response.status === 429) {
      const wait = Number(response.headers.get('Retry-After')) || 0;
      error.textContent = `Too many wrong passwords. Try again in ${wait} s.`;
    } else {
      error.textContent = `Login failed (HTTP ${response.status}).`;
    }
    form.password.select();
  } catch {
    error.textContent = 'The instance did not answer. Try again.';
  } finally {
    submit.disabled = false;
  }
});
