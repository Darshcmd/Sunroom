"use strict";

const config = window.SUNROOM_CONFIG || {};
const API_URL = String(config.apiUrl || "").replace(/\/$/, "");
const REGION = config.region || "";
const CLIENT_ID = config.userPoolClientId || "";
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;
const SESSION_KEY = "sunroom.session";

const els = {
  discoverView: document.getElementById("discover-view"),
  mineView: document.getElementById("my-photos-view"),
  gallery: document.getElementById("gallery-content"),
  galleryCount: document.getElementById("gallery-count"),
  galleryNotice: document.getElementById("gallery-notice"),
  myPhotos: document.getElementById("my-photos-content"),
  myNotice: document.getElementById("my-photos-notice"),
  authModal: document.getElementById("auth-modal"),
  uploadModal: document.getElementById("upload-modal"),
  viewerModal: document.getElementById("viewer-modal"),
  toast: document.getElementById("toast"),
  authButton: document.getElementById("auth-button"),
  signoutButton: document.getElementById("signout-button"),
  authForm: document.getElementById("auth-form"),
  confirmForm: document.getElementById("confirm-form"),
  authTitle: document.getElementById("auth-title"),
  authIntro: document.getElementById("auth-intro"),
  authSubmit: document.getElementById("auth-submit"),
  authName: document.getElementById("auth-name"),
  authNameField: document.getElementById("name-field"),
  authEmail: document.getElementById("auth-email"),
  authPassword: document.getElementById("auth-password"),
  authError: document.getElementById("auth-error"),
  authSwitch: document.getElementById("auth-switch"),
  authSwitchCopy: document.getElementById("auth-switch-copy"),
  confirmCode: document.getElementById("confirm-code"),
  confirmError: document.getElementById("confirm-error"),
  uploadForm: document.getElementById("upload-form"),
  photoFile: document.getElementById("photo-file"),
  fileLabel: document.getElementById("file-label"),
  previewWrap: document.getElementById("upload-preview-wrap"),
  preview: document.getElementById("upload-preview"),
  previewName: document.getElementById("preview-name"),
  photoCaption: document.getElementById("photo-caption"),
  uploadError: document.getElementById("upload-error"),
  uploadSubmit: document.getElementById("upload-submit"),
  dropArea: document.getElementById("drop-area"),
  viewerImage: document.getElementById("viewer-image"),
  viewerTitle: document.getElementById("viewer-title"),
  viewerOwner: document.getElementById("viewer-owner"),
  viewerDownload: document.getElementById("viewer-download")
};

const state = {
  view: "discover",
  gallery: [],
  myPhotos: [],
  session: readSession(),
  authMode: "signin",
  authStep: "form",
  confirmationEmail: "",
  selectedFile: null,
  previewUrl: "",
  lastFocused: null,
  toastTimer: 0,
  pollTimer: 0,
  isUploading: false
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function showToast(message, isError = false) {
  clearTimeout(state.toastTimer);
  els.toast.textContent = message;
  els.toast.classList.toggle("is-error", isError);
  els.toast.hidden = false;
  state.toastTimer = setTimeout(() => { els.toast.hidden = true; }, 4200);
}

function setNotice(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function apiConfigured() {
  return Boolean(API_URL && REGION && CLIENT_ID);
}

function configHelp() {
  return "Connect the AWS outputs in frontend/config.js to load the photo wall.";
}

function mediaUrl(path) {
  return new URL(path, API_URL).href;
}

function readSession() {
  try {
    const saved = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
    if (!saved?.idToken) return null;
    return { ...saved, profile: tokenClaims(saved.idToken) };
  } catch {
    return null;
  }
}

function tokenClaims(token) {
  try {
    const encoded = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return {};
  }
}

function saveSession(authResult, oldRefreshToken = "") {
  const next = {
    idToken: authResult.IdToken,
    accessToken: authResult.AccessToken,
    refreshToken: authResult.RefreshToken || oldRefreshToken,
    expiresAt: Date.now() + Number(authResult.ExpiresIn || 3600) * 1000
  };
  next.profile = tokenClaims(next.idToken);
  localStorage.setItem(SESSION_KEY, JSON.stringify(next));
  state.session = next;
  renderAccount();
  return next;
}

function clearSession() {
  localStorage.removeItem(SESSION_KEY);
  state.session = null;
  renderAccount();
}

async function cognitoRequest(action, payload) {
  if (!REGION || !CLIENT_ID) throw new Error(configHelp());
  const result = await fetch(`https://cognito-idp.${REGION}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "content-type": "application/x-amz-json-1.1",
      "x-amz-target": `AWSCognitoIdentityProviderService.${action}`
    },
    body: JSON.stringify({ ClientId: CLIENT_ID, ...payload })
  });
  const body = await result.json().catch(() => ({}));
  if (!result.ok) {
    const error = new Error(body.message || body.Message || "Your account request could not be completed.");
    error.code = String(body.__type || body.code || "").split("#").pop();
    throw error;
  }
  return body;
}

async function validSession() {
  const current = state.session;
  if (!current) return null;
  if (current.expiresAt > Date.now() + 60000) return current;
  if (!current.refreshToken) {
    clearSession();
    return null;
  }
  try {
    const result = await cognitoRequest("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: current.refreshToken }
    });
    return saveSession(result.AuthenticationResult, current.refreshToken);
  } catch {
    clearSession();
    return null;
  }
}

async function apiRequest(path, options = {}) {
  if (!API_URL) throw new Error(configHelp());
  const session = options.public ? null : await validSession();
  if (!options.public && !session) throw new Error("Sign in to continue.");
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (session) headers.set("authorization", `Bearer ${session.idToken}`);
  const result = await fetch(`${API_URL}${path}`, { ...options, headers });
  if (result.status === 204) return null;
  const body = await result.json().catch(() => ({}));
  if (!result.ok) throw new Error(body.message || "The request could not be completed.");
  return body;
}

function setView(view) {
  if (view === "mine" && !state.session) {
    state.view = "mine";
    setAuthMode("signin");
    showModal(els.authModal, els.authEmail);
    return;
  }
  state.view = view;
  els.discoverView.hidden = view !== "discover";
  els.mineView.hidden = view !== "mine";
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (view === "mine") loadMyPhotos();
  else loadGallery();
}

function renderAccount() {
  const signedIn = Boolean(state.session);
  els.authButton.hidden = signedIn;
  els.signoutButton.hidden = !signedIn;
  els.signoutButton.textContent = signedIn
    ? `Sign out${state.session.profile?.name ? `, ${state.session.profile.name.split(" ")[0]}` : ""}`
    : "Sign out";
  document.querySelector('[data-view="mine"]').classList.toggle("is-available", signedIn);
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(date);
}

function mediaAlt(photo) {
  const caption = photo.caption || "A photo from the community";
  return `${caption}, shared by ${photo.displayName || "a community member"}`;
}

function renderGallery() {
  els.gallery.setAttribute("aria-busy", "false");
  els.galleryCount.textContent = state.gallery.length ? `${state.gallery.length} ${state.gallery.length === 1 ? "photo" : "photos"}` : "";
  if (!state.gallery.length) {
    els.gallery.innerHTML = emptyGallery();
    return;
  }
  els.gallery.innerHTML = state.gallery.map((photo) => `
    <article class="photo-card">
      <button class="photo-open" type="button" data-photo-id="${escapeHtml(photo.photoId)}" aria-label="Open ${escapeHtml(mediaAlt(photo))}">
        <img src="${escapeHtml(mediaUrl(photo.urls.medium))}" alt="${escapeHtml(mediaAlt(photo))}" loading="lazy" decoding="async" />
      </button>
      <div class="photo-info">
        <p class="photo-caption">${escapeHtml(photo.caption || "A moment from the community")}</p>
        <span class="photo-byline">${escapeHtml(photo.displayName || "Member")}</span>
      </div>
    </article>
  `).join("");
}

function emptyGallery() {
  return `<div class="empty-state">
    <div class="empty-illustration" aria-hidden="true"><span class="empty-sun"></span><span class="empty-frame"><svg viewBox="0 0 32 32"><path d="M4 10.5A2.5 2.5 0 0 1 6.5 8h4l2-2.5h7L22 8h3.5a2.5 2.5 0 0 1 2.5 2.5v13a2.5 2.5 0 0 1-2.5 2.5h-17A2.5 2.5 0 0 1 6 23.5v-13Z"/><circle cx="16" cy="17" r="5"/></svg></span></div>
    <h3>The wall is ready for a first photo.</h3>
    <p>Share a photo and, once it passes the safety check, it will show up here for everyone.</p>
    <button class="button button-primary button-small" type="button" data-action="upload">Share a photo</button>
  </div>`;
}

async function loadGallery() {
  els.gallery.setAttribute("aria-busy", "true");
  els.gallery.innerHTML = '<div class="loading-state"><div class="loading-mark" aria-hidden="true"></div>Loading the photo wall</div>';
  setNotice(els.galleryNotice, "");
  try {
    const result = await apiRequest("/photos", { method: "GET", public: true });
    state.gallery = result.photos || [];
    renderGallery();
  } catch (error) {
    state.gallery = [];
    els.gallery.innerHTML = emptyGallery();
    setNotice(els.galleryNotice, error.message === configHelp() ? configHelp() : error.message);
  }
}

function statusText(photo) {
  if (photo.status === "UPLOADING" && photo.uploadExpiresAt && Date.parse(photo.uploadExpiresAt) < Date.now()) return "Upload expired";
  return ({
    UPLOADING: "Waiting for upload",
    PROCESSING: "Checking the photo",
    APPROVED: "On the public wall",
    REJECTED: "Not published",
    QUOTA_PAUSED: "Try again next month",
    FAILED: "Could not process",
    DELETING: "Removing"
  })[photo.status] || "Processing";
}

function photoStillActive(photo) {
  if (photo.status === "PROCESSING") return true;
  return photo.status === "UPLOADING" && (!photo.uploadExpiresAt || Date.parse(photo.uploadExpiresAt) > Date.now());
}

function renderMyPhotos() {
  els.myPhotos.setAttribute("aria-busy", "false");
  const photos = state.myPhotos;
  if (!photos.length) {
    els.myPhotos.innerHTML = `<div class="my-empty"><p>You have not shared a photo yet.</p><button class="button button-primary button-small" type="button" data-action="upload">Share a photo</button></div>`;
    return;
  }
  els.myPhotos.innerHTML = photos.map((photo) => {
    const title = photo.caption || "Untitled photo";
    const image = photo.status === "APPROVED"
      ? `<img class="my-photo-thumb" src="${escapeHtml(mediaUrl(photo.urls.small))}" alt="" loading="lazy" decoding="async" />`
      : '<span class="my-photo-placeholder" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M5 10.5A2.5 2.5 0 0 1 7.5 8h4l2-2.5h5L21.5 8h3A2.5 2.5 0 0 1 27 10.5v13a2.5 2.5 0 0 1-2.5 2.5h-17A2.5 2.5 0 0 1 5 23.5v-13Z"/><circle cx="16" cy="17" r="5"/></svg></span>';
    const reason = photo.moderationReason ? `<p class="my-photo-reason">${escapeHtml(photo.moderationReason)}</p>` : "";
    const remove = photo.canRemove
      ? `<button class="remove-button" type="button" data-remove-id="${escapeHtml(photo.photoId)}" aria-label="Remove ${escapeHtml(title)}">${photo.status === "DELETING" ? "Retry removal" : "Remove"}</button>`
      : "";
    const statusClass = `status-${String(photo.status).toLowerCase()}`;
    return `<article class="my-photo-row">
      ${image}
      <div class="my-photo-main">
        <p class="my-photo-title">${escapeHtml(title)}</p>
        <p class="my-photo-date">Added ${escapeHtml(formatDate(photo.createdAt))}</p>
        ${reason}
      </div>
      <div class="my-photo-actions">
        <span class="status-label ${statusClass}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(statusText(photo))}</span>
        ${remove}
      </div>
    </article>`;
  }).join("");
}

async function loadMyPhotos() {
  if (!state.session) {
    state.myPhotos = [];
    renderMyPhotos();
    setNotice(els.myNotice, "Sign in to see your uploads.");
    return;
  }
  els.myPhotos.setAttribute("aria-busy", "true");
  setNotice(els.myNotice, "");
  try {
    const result = await apiRequest("/my/photos", { method: "GET" });
    const saved = result.photos || [];
    const ids = new Set(saved.map((photo) => photo.photoId));
    const recentLocal = state.myPhotos.filter((photo) => !ids.has(photo.photoId) && Date.now() - Date.parse(photo.createdAt) < 120000);
    state.myPhotos = [...recentLocal, ...saved];
    renderMyPhotos();
    updatePolling();
  } catch (error) {
    if (error.message === "Sign in to continue.") clearSession();
    setNotice(els.myNotice, error.message);
    renderMyPhotos();
  }
}

function updatePolling() {
  clearInterval(state.pollTimer);
  if (state.view !== "mine" || !state.myPhotos.some(photoStillActive)) return;
  state.pollTimer = setInterval(loadMyPhotos, 4500);
}

function showModal(modal, focusTarget) {
  state.lastFocused = document.activeElement;
  modal.hidden = false;
  document.body.classList.add("has-modal");
  if (focusTarget) setTimeout(() => focusTarget.focus(), 20);
}

function closeModal(modal) {
  modal.hidden = true;
  if ([els.authModal, els.uploadModal, els.viewerModal].every((entry) => entry.hidden)) {
    document.body.classList.remove("has-modal");
  }
  if (state.lastFocused?.focus) state.lastFocused.focus();
}

function setAuthMode(mode) {
  state.authMode = mode;
  state.authStep = "form";
  els.authForm.hidden = false;
  els.confirmForm.hidden = true;
  els.authError.hidden = true;
  const signingUp = mode === "signup";
  els.authTitle.textContent = signingUp ? "Create your account" : "Sign in to share";
  els.authIntro.textContent = signingUp
    ? "Your name and caption appear with photos approved for the public wall."
    : "Your photos are checked before they join the public wall.";
  els.authNameField.hidden = !signingUp;
  els.authName.required = signingUp;
  els.authPassword.autocomplete = signingUp ? "new-password" : "current-password";
  els.authSubmit.textContent = signingUp ? "Create account" : "Sign in";
  els.authSwitchCopy.textContent = signingUp ? "Already have an account?" : "New to Sunroom?";
  els.authSwitch.textContent = signingUp ? "Sign in" : "Create an account";
}

function showConfirmation(email) {
  state.authStep = "confirm";
  state.confirmationEmail = email;
  els.authForm.hidden = true;
  els.confirmForm.hidden = false;
  els.confirmError.hidden = true;
  els.confirmCode.value = "";
  els.confirmCode.focus();
}

function setFormError(element, message) {
  element.textContent = message;
  element.hidden = !message;
}

async function handleAuthSubmit(event) {
  event.preventDefault();
  setFormError(els.authError, "");
  const email = els.authEmail.value.trim();
  const password = els.authPassword.value;
  els.authSubmit.disabled = true;
  try {
    if (state.authMode === "signup") {
      await cognitoRequest("SignUp", {
        Username: email,
        Password: password,
        UserAttributes: [
          { Name: "email", Value: email },
          { Name: "name", Value: els.authName.value.trim() }
        ]
      });
      showConfirmation(email);
      return;
    }
    const result = await cognitoRequest("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      AuthParameters: { USERNAME: email, PASSWORD: password }
    });
    if (!result.AuthenticationResult) throw new Error("Complete the account challenge before signing in.");
    saveSession(result.AuthenticationResult);
    closeModal(els.authModal);
    showToast("You are signed in.");
    if (state.view === "mine") loadMyPhotos();
  } catch (error) {
    if (error.code === "UserNotConfirmedException") {
      showConfirmation(email);
      return;
    }
    setFormError(els.authError, error.message);
  } finally {
    els.authSubmit.disabled = false;
  }
}

async function handleConfirmSubmit(event) {
  event.preventDefault();
  setFormError(els.confirmError, "");
  try {
    await cognitoRequest("ConfirmSignUp", {
      Username: state.confirmationEmail,
      ConfirmationCode: els.confirmCode.value.trim()
    });
    setAuthMode("signin");
    els.authEmail.value = state.confirmationEmail;
    els.authPassword.focus();
    showToast("Email confirmed. Sign in to continue.");
  } catch (error) {
    setFormError(els.confirmError, error.message);
  }
}

async function resendCode() {
  setFormError(els.confirmError, "");
  try {
    await cognitoRequest("ResendConfirmationCode", { Username: state.confirmationEmail });
    showToast("A new code is on its way.");
  } catch (error) {
    setFormError(els.confirmError, error.message);
  }
}

function openAuth() {
  setAuthMode("signin");
  showModal(els.authModal, els.authEmail);
}

function openUpload() {
  if (!state.session) {
    showToast("Sign in to share a photo.");
    openAuth();
    return;
  }
  if (!apiConfigured()) {
    showToast(configHelp(), true);
    return;
  }
  resetUploadForm();
  showModal(els.uploadModal, els.photoFile);
}

function resetUploadForm() {
  els.uploadForm.reset();
  setFormError(els.uploadError, "");
  els.previewWrap.hidden = true;
  els.dropArea.hidden = false;
  els.fileLabel.textContent = "Choose a photo";
  state.selectedFile = null;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = "";
}

function selectFile(file) {
  setFormError(els.uploadError, "");
  if (!file) return;
  const contentType = file.type || (/\.png$/i.test(file.name) ? "image/png" : "image/jpeg");
  if (!(contentType in { "image/jpeg": true, "image/png": true })) {
    setFormError(els.uploadError, "Choose a JPEG or PNG photo.");
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    setFormError(els.uploadError, "Photos must be 2 MiB or smaller.");
    return;
  }
  state.selectedFile = file;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = URL.createObjectURL(file);
  els.preview.src = state.previewUrl;
  els.previewName.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MiB`;
  els.previewWrap.hidden = false;
  els.dropArea.hidden = true;
}

async function submitPhoto(event) {
  event.preventDefault();
  if (!state.selectedFile || state.isUploading) return;
  setFormError(els.uploadError, "");
  state.isUploading = true;
  els.uploadSubmit.disabled = true;
  els.uploadSubmit.textContent = "Uploading…";
  try {
    const file = state.selectedFile;
    const contentType = file.type || (/\.png$/i.test(file.name) ? "image/png" : "image/jpeg");
    const result = await apiRequest("/uploads", {
      method: "POST",
      body: JSON.stringify({ contentType, fileSize: file.size, caption: els.photoCaption.value })
    });
    state.myPhotos = [result.photo, ...state.myPhotos.filter((photo) => photo.photoId !== result.photo.photoId)];
    state.view = "mine";
    updateViewChrome();
    renderMyPhotos();

    const uploaded = await fetch(result.upload.url, {
      method: "PUT",
      headers: { "Content-Type": contentType, "If-None-Match": "*" },
      body: file
    });
    if (!uploaded.ok) throw new Error("The upload did not finish. Remove this pending photo from My photos and try again.");

    closeModal(els.uploadModal);
    showToast(result.notificationConfirmationRequested
      ? "Photo uploaded. Check your email to confirm processing notifications."
      : "Photo uploaded. It will appear on the wall after the safety check.");
    await loadMyPhotos();
  } catch (error) {
    setFormError(els.uploadError, error.message);
    if (state.myPhotos.some((photo) => photo.status === "UPLOADING")) loadMyPhotos();
  } finally {
    state.isUploading = false;
    els.uploadSubmit.disabled = false;
    els.uploadSubmit.textContent = "Share photo";
  }
}

function updateViewChrome() {
  els.discoverView.hidden = state.view !== "discover";
  els.mineView.hidden = state.view !== "mine";
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === state.view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function openViewer(photoId) {
  const photo = state.gallery.find((item) => item.photoId === photoId);
  if (!photo) return;
  els.viewerImage.src = mediaUrl(photo.urls.large);
  els.viewerImage.alt = mediaAlt(photo);
  els.viewerTitle.textContent = photo.caption || "A moment from the community";
  els.viewerOwner.textContent = `${photo.displayName || "Member"} · ${formatDate(photo.createdAt)}`;
  els.viewerDownload.href = mediaUrl(photo.urls.large);
  showModal(els.viewerModal, document.querySelector(".viewer-close"));
}

async function removePhoto(photoId, button) {
  const photo = state.myPhotos.find((item) => item.photoId === photoId);
  if (!photo) return;
  if (!window.confirm("Remove this photo and free its space on the wall?")) return;
  button.disabled = true;
  try {
    await apiRequest(`/my/photos/${encodeURIComponent(photoId)}`, { method: "DELETE" });
    state.myPhotos = state.myPhotos.filter((item) => item.photoId !== photoId);
    renderMyPhotos();
    showToast("Photo removed.");
    if (state.view === "discover") loadGallery();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) {
    setView(viewButton.dataset.view);
    return;
  }
  const actionButton = event.target.closest("[data-action]");
  if (actionButton?.dataset.action === "upload") {
    openUpload();
    return;
  }
  if (actionButton?.dataset.action === "auth") {
    openAuth();
    return;
  }
  const closeButton = event.target.closest("[data-close]");
  if (closeButton) {
    const modal = { auth: els.authModal, upload: els.uploadModal, viewer: els.viewerModal }[closeButton.dataset.close];
    if (modal && !(modal === els.uploadModal && state.isUploading)) closeModal(modal);
    return;
  }
  const photoButton = event.target.closest("[data-photo-id]");
  if (photoButton) {
    openViewer(photoButton.dataset.photoId);
    return;
  }
  const removeButton = event.target.closest("[data-remove-id]");
  if (removeButton) removePhoto(removeButton.dataset.removeId, removeButton);
});

els.authForm.addEventListener("submit", handleAuthSubmit);
els.confirmForm.addEventListener("submit", handleConfirmSubmit);
els.uploadForm.addEventListener("submit", submitPhoto);
els.authSwitch.addEventListener("click", () => setAuthMode(state.authMode === "signin" ? "signup" : "signin"));
document.getElementById("resend-code").addEventListener("click", resendCode);
els.signoutButton.addEventListener("click", () => {
  clearSession();
  state.myPhotos = [];
  if (state.view === "mine") setView("discover");
  showToast("You are signed out.");
});
els.photoFile.addEventListener("change", () => selectFile(els.photoFile.files?.[0]));
document.getElementById("clear-photo").addEventListener("click", resetUploadForm);
els.dropArea.addEventListener("dragover", (event) => { event.preventDefault(); els.dropArea.classList.add("is-dragging"); });
els.dropArea.addEventListener("dragleave", () => els.dropArea.classList.remove("is-dragging"));
els.dropArea.addEventListener("drop", (event) => {
  event.preventDefault();
  els.dropArea.classList.remove("is-dragging");
  selectFile(event.dataTransfer.files?.[0]);
});
document.addEventListener("keydown", (event) => {
  const modal = [els.viewerModal, els.uploadModal, els.authModal].find((entry) => !entry.hidden);
  if (!modal) return;
  if (event.key === "Escape") {
    if (modal !== els.uploadModal || !state.isUploading) closeModal(modal);
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...modal.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter((element) => element.getClientRects().length > 0);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
    event.preventDefault();
    first.focus();
  }
});

renderAccount();
updateViewChrome();
if (state.session && state.session.expiresAt <= Date.now() && !state.session.refreshToken) clearSession();
loadGallery();
