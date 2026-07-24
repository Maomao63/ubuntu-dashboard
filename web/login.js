const form = document.querySelector("#login-form");
const error = document.querySelector("#login-error");
form.addEventListener("submit", async event => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        username: document.querySelector("#username").value,
        password: document.querySelector("#password").value
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Sign in failed");
    location.replace("/");
  } catch (exception) {
    error.textContent = exception.message;
    document.querySelector("#password").value = "";
    document.querySelector("#password").focus();
  } finally {
    button.disabled = false;
  }
});
