document.addEventListener(
    "DOMContentLoaded",
    function () {


        const layout =
            document.getElementById(
                "workerLayout"
            );


        const sidebarToggle =
            document.getElementById(
                "sidebarToggle"
            );


        const mobileMenuBtn =
            document.getElementById(
                "mobileMenuBtn"
            );


        const sidebarOverlay =
            document.getElementById(
                "sidebarOverlay"
            );


        /* =========================
           DESKTOP MINIMIZE
        ========================= */

        if (sidebarToggle) {

            sidebarToggle.addEventListener(
                "click",
                function () {

                    if (
                        window.innerWidth > 768
                    ) {

                        layout.classList.toggle(
                            "sidebar-mini"
                        );

                    }

                }
            );

        }


        /* =========================
           MOBILE OPEN
        ========================= */

        if (mobileMenuBtn) {

            mobileMenuBtn.addEventListener(
                "click",
                function () {

                    layout.classList.add(
                        "sidebar-open"
                    );

                }
            );

        }


        /* =========================
           MOBILE CLOSE
        ========================= */

        if (sidebarOverlay) {

            sidebarOverlay.addEventListener(
                "click",
                function () {

                    layout.classList.remove(
                        "sidebar-open"
                    );

                }
            );

        }


        /* =========================
           CLOSE AFTER LINK
        ========================= */

        document
            .querySelectorAll(".sidebar-link")
            .forEach(
                function (link) {

                    link.addEventListener(
                        "click",
                        function () {

                            if (
                                window.innerWidth <= 768
                            ) {

                                layout.classList.remove(
                                    "sidebar-open"
                                );

                            }

                        }
                    );

                }
            );


        /* =========================
           RESIZE
        ========================= */

        window.addEventListener(
            "resize",
            function () {

                if (
                    window.innerWidth > 768
                ) {

                    layout.classList.remove(
                        "sidebar-open"
                    );

                }

            }
        );

    }
);