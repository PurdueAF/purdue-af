const vscode = require("vscode");

function normalizeBasePath(path) {
  if (!path || path === "/") {
    return "";
  }
  return path.endsWith("/") ? path.slice(0, -1) : path;
}

function buildUrls() {
  const config = vscode.workspace.getConfiguration("purdueaf");
  const hubOrigin = normalizeBasePath(config.get("hubOrigin") || "");
  const hubBase = normalizeBasePath(process.env.JUPYTERHUB_BASE_URL || "/");
  const servicePrefix = process.env.JUPYTERHUB_SERVICE_PREFIX || "";
  const labPath =
    config.get("jupyterLabPath") ||
    `${servicePrefix}lab/tree`.replace(/^\/+/, "/");
  const shutdownPath = config.get("shutdownPath") || "/hub/home";

  function toTarget(path) {
    if (/^https?:\/\//i.test(path)) {
      return path;
    }
    const normalized = path.startsWith("/") ? path : `/${path}`;
    const origin = hubOrigin || (hubBase.startsWith("http") ? hubBase : "");
    return origin ? `${origin}${normalized}` : normalized;
  }

  return {
    labUrl: toTarget(labPath),
    shutdownUrl: toTarget(shutdownPath),
  };
}

async function navigateTo(target) {
  // openExternal is unreliable in browser code-server behind JupyterHub proxies.
  // Redirect via a short-lived webview that runs in the browser context.
  const panel = vscode.window.createWebviewPanel(
    "purdueafNavigate",
    "",
    { viewColumn: vscode.ViewColumn.Active, preserveFocus: false },
    { enableScripts: true }
  );

  panel.webview.html = `<!DOCTYPE html>
<html>
  <head><meta charset="UTF-8"></head>
  <body>
    <script>
      (function () {
        var target = ${JSON.stringify(target)};
        var url = target;
        if (!/^https?:\\/\\//i.test(target)) {
          var topWindow = window.top || window;
          var origin = topWindow.location.origin;
          url = origin + (target.startsWith("/") ? target : "/" + target);
        }
        (window.top || window).location.href = url;
      })();
    </script>
  </body>
</html>`;

  setTimeout(() => {
    panel.dispose();
  }, 250);
}

function activate(context) {
  const { labUrl, shutdownUrl } = buildUrls();

  const switchToJupyterLab = vscode.commands.registerCommand(
    "purdueaf.switchToJupyterLab",
    async () => {
      await navigateTo(labUrl);
    }
  );

  const openShutdownPage = vscode.commands.registerCommand(
    "purdueaf.openShutdownPage",
    async () => {
      await navigateTo(shutdownUrl);
    }
  );

  const labButton = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 200);
  labButton.name = "Purdue AF JupyterLab Button";
  labButton.text = "$(notebook) JupyterLab";
  labButton.tooltip = "Switch to JupyterLab interface";
  labButton.command = "purdueaf.switchToJupyterLab";
  labButton.show();

  const shutdownButton = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    199
  );
  shutdownButton.name = "Purdue AF Shutdown Button";
  shutdownButton.text = "$(power) Shut Down";
  shutdownButton.tooltip = "Open JupyterHub page to stop this server";
  shutdownButton.command = "purdueaf.openShutdownPage";
  shutdownButton.show();

  context.subscriptions.push(
    switchToJupyterLab,
    openShutdownPage,
    labButton,
    shutdownButton
  );
}

function deactivate() {}

module.exports = {
  activate,
  deactivate,
};
