// On narrow screens the nav scrolls horizontally; keep the current section in view.
document.querySelector('.manager-nav-link.active')?.scrollIntoView({block: 'nearest', inline: 'center'});
