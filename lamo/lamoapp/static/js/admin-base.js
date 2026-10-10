

document.addEventListener("DOMContentLoaded", function () {
    const sidebar = document.getElementById("sidebar");
    const sidebarToggle = document.getElementById("sidebarToggle");
    const sidebarOverlay = document.getElementById("sidebarOverlay");
    const sidebarCollapseBtn = document.getElementById("sidebarCollapseBtn");

    if (sidebarCollapseBtn) {
        sidebarCollapseBtn.addEventListener("click", function () {
            if (window.innerWidth > 768) {
                document.body.classList.toggle("sidebar-collapsed");
                const isCollapsed = document.body.classList.contains("sidebar-collapsed");
                localStorage.setItem("adminSidebarCollapsed", isCollapsed ? "true" : "false");
                sidebarCollapseBtn.title = isCollapsed ? "Expand Sidebar" : "Minimize Sidebar";
            }
        });
    }

    if (window.innerWidth > 768 && localStorage.getItem("adminSidebarCollapsed") === "true") {
        document.body.classList.add("sidebar-collapsed");
        if (sidebarCollapseBtn) sidebarCollapseBtn.title = "Expand Sidebar";
    }

    if (sidebarToggle) {
        sidebarToggle.addEventListener("click", function () {
            sidebar.classList.toggle("show");
            sidebarOverlay.classList.toggle("show");
        });
    }

    if (sidebarOverlay) {
        sidebarOverlay.addEventListener("click", function () {
            sidebar.classList.remove("show");
            sidebarOverlay.classList.remove("show");
        });
    }

    document.querySelectorAll(".sidebar-menu a").forEach(function (link) {
        link.addEventListener("click", function () {
            if (window.innerWidth <= 768) {
                sidebar.classList.remove("show");
                sidebarOverlay.classList.remove("show");
            }
        });
    });

    window.addEventListener("resize", function () {
        if (window.innerWidth <= 768) {
            document.body.classList.remove("sidebar-collapsed");
        } else {
            if (localStorage.getItem("adminSidebarCollapsed") === "true") {
                document.body.classList.add("sidebar-collapsed");
            }
        }
    });
});