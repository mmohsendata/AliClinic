const menuToggle = document.querySelector("#menuToggle");
const sidebar = document.querySelector("#sidebar");

if (menuToggle && sidebar) {
  menuToggle.addEventListener("click", () => sidebar.classList.toggle("open"));
  document.addEventListener("click", (event) => {
    if (window.innerWidth <= 780 && !sidebar.contains(event.target) && !menuToggle.contains(event.target)) {
      sidebar.classList.remove("open");
    }
  });
}
