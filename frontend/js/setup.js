// The Device setup guide's Camera switch (#100): shows the lines marked for the
// chosen variant (data-variant lists them) and hides the rest. Without this
// module the page still reads, showing the USB path it is written with.

const options = [...document.querySelectorAll('[data-choose]')];
const lines = [...document.querySelectorAll('[data-variant]')];

function choose(variant) {
  for (const option of options) option.setAttribute('aria-pressed', String(option.dataset.choose === variant));
  for (const line of lines) line.hidden = !line.dataset.variant.split(' ').includes(variant);
}

for (const option of options) option.addEventListener('click', () => choose(option.dataset.choose));
