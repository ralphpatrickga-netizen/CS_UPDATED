(function () {
  function renderPreview(files, container) {
    container.innerHTML = "";
    Array.from(files).forEach((file) => {
      const card = document.createElement("div");
      card.className = "form-attachment-card";
      if (file.type.startsWith("image/")) {
        const img = document.createElement("img");
        img.alt = file.name;
        const reader = new FileReader();
        reader.onload = (e) => { img.src = e.target.result; };
        reader.readAsDataURL(file);
        card.appendChild(img);
      } else {
        card.classList.add("form-attachment-pdf");
        const icon = document.createElement("span");
        icon.className = "pdf-icon";
        icon.textContent = "📄";
        card.appendChild(icon);
      }
      container.appendChild(card);
    });
  }

  function setup(input) {
    const previewId = input.dataset.previewFor;
    if (!previewId) return;
    const container = document.getElementById(previewId);
    if (!container) return;
    input.addEventListener("change", () => renderPreview(input.files, container));
  }

  function setupProfilePreview(input) {
    const container = document.getElementById(input.dataset.profilePreview);
    const message = document.getElementById(input.dataset.profileError);
    if (!container || !message) return;

    const initialPreview = container.innerHTML;
    const maxSize = Number(input.dataset.maxSize);
    const maxSizeLabel = input.dataset.maxSizeLabel;
    let previewUrl = null;

    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;

      const extensionIsAllowed = /\.(jpe?g|png)$/i.test(file.name);
      const mimeIsAllowed = !file.type || ["image/jpeg", "image/png"].includes(file.type);
      let error = "";

      if (!extensionIsAllowed || !mimeIsAllowed) {
        error = "Please choose a JPG or PNG image.";
      } else if (file.size > maxSize) {
        error = `Image must be ${maxSizeLabel} or smaller.`;
      }

      if (error) {
        input.value = "";
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
        container.innerHTML = initialPreview;
        message.textContent = error;
        message.classList.add("is-error");
        return;
      }

      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      const img = document.createElement("img");
      img.src = previewUrl;
      img.alt = "Selected profile picture preview";
      img.className = "profile-avatar-lg";
      container.replaceChildren(img);
      message.textContent = "";
      message.classList.remove("is-error");
    });
  }

  document.querySelectorAll("input[type=file][data-preview-for]").forEach(setup);
  document.querySelectorAll("input[type=file][data-profile-preview]").forEach(setupProfilePreview);

  window.BCFilePreview = { setup: setup };
})();
