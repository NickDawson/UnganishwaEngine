try {
    document.documentElement.classList.toggle('dark', localStorage.getItem('unganishwa-admin-theme') === 'dark');
} catch {}

document.addEventListener('DOMContentLoaded', () => {
    const theme = document.querySelector('[data-theme-toggle]');
    theme?.setAttribute('aria-pressed', String(document.documentElement.classList.contains('dark')));
    theme?.addEventListener('click', () => {
        const dark = document.documentElement.classList.toggle('dark');
        theme.setAttribute('aria-pressed', String(dark));
        try {
            localStorage.setItem('unganishwa-admin-theme', dark ? 'dark' : 'light');
        } catch {}
    });

    const sidebar = document.querySelector('#sidebar');
    const menu = document.querySelector('[data-nav-toggle]');
    const close = document.querySelector('[data-nav-close]');
    const setMenu = (open) => {
        sidebar.toggleAttribute('data-open', open);
        menu.setAttribute('aria-expanded', String(open));
        (open ? close : menu).focus();
    };
    menu?.addEventListener('click', () => setMenu(!sidebar.hasAttribute('data-open')));
    close?.addEventListener('click', () => setMenu(false));
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && sidebar?.hasAttribute('data-open')) setMenu(false);
    });
    document.addEventListener('pointerdown', (event) => {
        if (sidebar?.hasAttribute('data-open') && !sidebar.contains(event.target) && !menu.contains(event.target)) setMenu(false);
    });

    const reveal = document.querySelector('[data-password-toggle]');
    reveal?.addEventListener('click', () => {
        const password = document.getElementById(reveal.getAttribute('aria-controls'));
        const visible = password.type === 'password';
        password.type = visible ? 'text' : 'password';
        reveal.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
        reveal.setAttribute('aria-pressed', String(visible));
    });

    const video = document.querySelector('[data-auth-video]');
    if (video) {
        const motion = matchMedia('(prefers-reduced-motion: reduce)');
        const syncVideo = () => motion.matches ? video.pause() : video.play().catch(() => {});
        motion.addEventListener('change', syncVideo);
        syncVideo();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    const collapse = document.querySelector('[data-nav-collapse]');
    collapse?.addEventListener('click', () => {
        const collapsed = document.querySelector('.admin-shell').toggleAttribute('data-nav-collapsed');
        collapse.setAttribute('aria-expanded', String(!collapsed));
        collapse.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
    });

    const search = document.querySelector('#admin-search');
    if (search) {
        document.addEventListener('keydown', (event) => {
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                search.focus();
            }
        });
    }

    const fullscreen = document.querySelector('[data-fullscreen]');
    if (fullscreen && document.fullscreenEnabled) {
        fullscreen.hidden = false;
        fullscreen.addEventListener('click', async () => {
            try {
                if (document.fullscreenElement) await document.exitFullscreen();
                else await document.documentElement.requestFullscreen();
            } catch {
                fullscreen.hidden = true;
            }
        });
        document.addEventListener('fullscreenchange', () => {
            fullscreen.setAttribute('aria-label', document.fullscreenElement ? 'Exit fullscreen' : 'Enter fullscreen');
        });
    }

    const menus = [...document.querySelectorAll('details.admin-account')];
    menus.forEach((menu) => menu.addEventListener('toggle', () => {
        if (menu.open) menus.filter((other) => other !== menu).forEach((other) => { other.open = false; });
    }));
    document.addEventListener('click', (event) => {
        menus.filter((menu) => !menu.contains(event.target)).forEach((menu) => { menu.open = false; });
    });
});
