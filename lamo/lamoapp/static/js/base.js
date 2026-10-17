/* =====================================================
       AOS
    ====================================================== */

    const prefersReducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    AOS.init({

      disable: prefersReducedMotion,

      duration: 800,

      easing: 'ease-in-out',

      once: true,

      offset: 50

    });






    /* =====================================================
       MOBILE DRAWER BACKDROP & ESCAPE CONTROLLER
    ====================================================== */
    document.addEventListener("DOMContentLoaded", function () {
        const mainNav = document.getElementById("mainNav");
        const backdrop = document.getElementById("mobileNavBackdrop");
        if (!mainNav || !backdrop) return;

        function closeNav() {
            if (window.bootstrap && window.bootstrap.Collapse) {
                const bsCollapse = window.bootstrap.Collapse.getInstance(mainNav);
                if (bsCollapse) {
                    bsCollapse.hide();
                } else {
                    mainNav.classList.remove("show");
                    backdrop.classList.remove("show");
                    document.body.style.overflow = "";
                }
            } else {
                mainNav.classList.remove("show");
                backdrop.classList.remove("show");
                document.body.style.overflow = "";
            }
        }

        mainNav.addEventListener("show.bs.collapse", function () {
            backdrop.classList.add("show");
            document.body.style.overflow = "hidden";
        });

        mainNav.addEventListener("hidden.bs.collapse", function () {
            backdrop.classList.remove("show");
            document.body.style.overflow = "";
        });

        backdrop.addEventListener("click", closeNav);

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && mainNav.classList.contains("show")) {
                closeNav();
            }
        });
    });

    /* =====================================================
       WORKER NAVBAR SCROLL EFFECT
       Transparent at top -> white on scroll.
    ====================================================== */
    document.addEventListener(
      "DOMContentLoaded",
      function () {

        const workerNavbar =
          document.getElementById("workerNavbar");

        if (!workerNavbar) return;


        function updateWorkerNavbar() {

          if (window.scrollY > 50) {

            workerNavbar.classList.add("scrolled");

          } else {

            workerNavbar.classList.remove("scrolled");

          }

        }


        window.addEventListener(
          "scroll",
          updateWorkerNavbar
        );


        updateWorkerNavbar();

      }
    );

    /* =====================================================
         AUTHENTICATED USER DROPDOWN
         Dashboard + Logout stay inside the user menu.
      ====================================================== */

      document.addEventListener(
        "DOMContentLoaded",
        function () {

          const userMenu =
            document.getElementById("userMenu");

          const userMenuTrigger =
            document.getElementById("userMenuTrigger");

          if (!userMenu || !userMenuTrigger) return;


          function closeUserMenu() {

            userMenu.classList.remove("show");

            userMenuTrigger.setAttribute(
              "aria-expanded",
              "false"
            );

          }


          userMenuTrigger.addEventListener(
            "click",
            function (event) {

              event.stopPropagation();

              const isOpen =
                userMenu.classList.toggle("show");

              userMenuTrigger.setAttribute(
                "aria-expanded",
                isOpen ? "true" : "false"
              );

            }
          );


          document.addEventListener(
            "click",
            function (event) {

              if (!userMenu.contains(event.target)) {

                closeUserMenu();

              }

            }
          );


          document.addEventListener(
            "keydown",
            function (event) {

              if (event.key === "Escape") {

                closeUserMenu();

              }

            }
          );

        }
      );



      /* =====================================================
       NAVBAR SCROLL EFFECT
    ====================================================== */

    window.addEventListener(
      "scroll",
      function () {

        const navbar =
          document.querySelector(
            ".navbar"
          );


        if (!navbar) return;


        if (window.scrollY > 40) {

          navbar.classList.add(
            "scrolled"
          );

        }

        else {

          navbar.classList.remove(
            "scrolled"
          );

        }

      }
    );



    /* =====================================================
       NAVBAR ACTIVE SECTION

       This handles:

       Home
       Services
       Workers
       Employers
       Contact
    ====================================================== */

    document.addEventListener(
      "DOMContentLoaded",
      function () {


        const sectionLinks =
          document.querySelectorAll(
            ".nav-link[data-section]"
          );


        const sections =
          document.querySelectorAll(
            "[id]"
          );


        function updateActiveSection() {


          /*
           * Do not run this logic on About page
           * or other pages unless a section exists.
           */

          const homeSections =
            document.querySelectorAll(
              "#services, #workers, #employers, #contact"
            );


          if (
            homeSections.length === 0
          ) {

            return;

          }


          let currentSection = "";


          const scrollPosition =
            window.scrollY + 180;


          homeSections.forEach(
            function (section) {

              const sectionTop =
                section.offsetTop;

              const sectionBottom =
                sectionTop +
                section.offsetHeight;


              if (
                scrollPosition >= sectionTop &&
                scrollPosition < sectionBottom
              ) {

                currentSection =
                  section.id;

              }

            }
          );


          /*
           * Remove active from
           * section navigation.
           */

          sectionLinks.forEach(
            function (link) {

              link.classList.remove(
                "active"
              );

            }
          );


          /*
           * Add active to
           * current section.
           */

          if (currentSection) {

            sectionLinks.forEach(
              function (link) {

                if (
                  link.dataset.section ===
                  currentSection
                ) {

                  link.classList.add(
                    "active"
                  );

                }

              }
            );

          }

        }


        window.addEventListener(
          "scroll",
          updateActiveSection
        );


        updateActiveSection();


        /* =========================================
           CLICK EFFECT
        ========================================== */

        sectionLinks.forEach(
          function (link) {

            link.addEventListener(
              "click",
              function () {


                sectionLinks.forEach(
                  function (item) {

                    item.classList.remove(
                      "active"
                    );

                  }
                );


                this.classList.add(
                  "active"
                );

              }
            );

          }
        );


      }
    );



    /* =====================================================
       NAVBAR PAGE ACTIVE STATE

       This makes sure when we click:

       Home     -> Home active
       About    -> About active
       My Bookings -> My Bookings active

       Django handles this using the
       request.resolver_match.url_name
       in the HTML above.
    ====================================================== */


    /* =====================================================
       SMOOTH SECTION NAVIGATION
    ====================================================== */

    document.addEventListener(
      "DOMContentLoaded",
      function () {


        const hashLinks =
          document.querySelectorAll(
            'a[href*="#"]'
          );


        hashLinks.forEach(
          function (link) {

            link.addEventListener(
              "click",
              function (event) {


                const href =
                  this.getAttribute(
                    "href"
                  );


                if (!href) return;


                const hashIndex =
                  href.indexOf("#");


                if (
                  hashIndex === -1
                ) {

                  return;

                }


                const hash =
                  href.substring(
                    hashIndex
                  );


                const target =
                  document.querySelector(
                    hash
                  );


                /*
                 * Only smooth-scroll if
                 * target exists on current page.
                 */

                if (target) {

                  event.preventDefault();


                  const navbarHeight =
                    document.querySelector(
                      ".navbar"
                    )?.offsetHeight || 80;


                  const targetPosition =
                    target.getBoundingClientRect()
                      .top
                    +
                    window.scrollY
                    -
                    navbarHeight
                    -
                    25;


                  window.scrollTo({

                    top:
                      targetPosition,

                    behavior:
                      "smooth"

                  });

                }

              }
            );

          }
        );

      }
    );