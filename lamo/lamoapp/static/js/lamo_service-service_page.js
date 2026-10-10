document.addEventListener("DOMContentLoaded", function () {

    const buttons = document.querySelectorAll(".filter-btn");
    const cards = document.querySelectorAll(".service-card");
    const grid = document.getElementById("servicesGrid");
    const search = document.getElementById("serviceSearch");

    /* ---------- FILTERS: animated instead of abrupt display:none ---------- */
    function applyFilter(filter) {
        cards.forEach((card, index) => {
            const featured = card.dataset.featured === "true";
            const matches = filter === "all" || filter === "available" ||
                            (filter === "featured" && featured);

            card.classList.remove("filter-show");

            if (matches) {
                card.classList.remove("filter-hide");
                card.style.position = "";
                card.style.animationDelay = `${index * 45}ms`;
                void card.offsetWidth;
                card.classList.add("filter-show");
            } else {
                card.classList.add("filter-hide");
                card.style.position = "absolute";
            }
        });

        /* Keep the grid's natural height when filtering */
        if (grid) {
            const visible = [...cards].filter(c => !c.classList.contains("filter-hide"));
            grid.style.minHeight = visible.length ? "" : "260px";
        }
    }

    buttons.forEach(button => {
        button.addEventListener("click", function () {
            buttons.forEach(btn => btn.classList.remove("active"));
            this.classList.add("active");
            applyFilter(this.dataset.filter);
        });
    });

    /* ---------- LIVE SEARCH ---------- */
    if (search) {
        search.addEventListener("input", function () {
            const value = this.value.toLowerCase().trim();

            cards.forEach((card, index) => {
                const name = card.querySelector("h3")?.textContent.toLowerCase() || "";
                const description = card.querySelector(".service-description")?.textContent.toLowerCase() || "";
                const match = !value || name.includes(value) || description.includes(value);

                if (match) {
                    card.classList.remove("filter-hide");
                    card.style.position = "";
                    card.style.animationDelay = `${index * 35}ms`;
                    void card.offsetWidth;
                    card.classList.add("filter-show");
                } else {
                    card.classList.remove("filter-show");
                    card.classList.add("filter-hide");
                    card.style.position = "absolute";
                }
            });
        });
    }

    /* ---------- POINTER TILT: subtle premium card interaction ---------- */
    const finePointer = window.matchMedia("(pointer:fine)").matches;

    if (finePointer) {
        cards.forEach(card => {
            card.addEventListener("pointermove", function (e) {
                if (card.classList.contains("filter-hide")) return;

                const rect = card.getBoundingClientRect();
                const x = e.clientX - rect.left;
                const y = e.clientY - rect.top;
                const rotateY = ((x / rect.width) - .5) * 5;
                const rotateX = ((y / rect.height) - .5) * -5;

                card.style.transform =
                    `translateY(-11px) rotateX(${rotateX}deg) rotateY(${rotateY}deg)`;
            });

            card.addEventListener("pointerleave", function () {
                card.style.transform = "";
            });
        });
    }

    /* ---------- BUTTON RIPPLE ---------- */
    document.querySelectorAll(".view-service, .cta-btn, .hero-search button, .filter-btn")
        .forEach(button => {
            button.addEventListener("click", function (e) {
                const rect = this.getBoundingClientRect();
                const ripple = document.createElement("span");
                const size = Math.max(rect.width, rect.height);

                ripple.style.cssText = `
                    position:absolute;
                    width:${size}px;
                    height:${size}px;
                    left:${e.clientX - rect.left - size/2}px;
                    top:${e.clientY - rect.top - size/2}px;
                    border-radius:50%;
                    background:rgba(255,255,255,.28);
                    transform:scale(0);
                    pointer-events:none;
                    animation:ripple .55s ease-out forwards;
                `;

                this.appendChild(ripple);
                setTimeout(() => ripple.remove(), 600);
            });
        });

    /* ---------- SCROLL REVEAL ---------- */
    if ("IntersectionObserver" in window) {
        const observer = new IntersectionObserver((entries, obs) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add("in-view");
                    obs.unobserve(entry.target);
                }
            });
        }, {threshold:.12});

        document.querySelectorAll(".services-heading, .service-cta")
            .forEach(el => observer.observe(el));
    }

});