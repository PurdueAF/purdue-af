const vscode = require("vscode");

function normalizeBasePath(path) {
  if (!path || path === "/") {
    return "";
  }
  return path.endsWith("/") ? path.slice(0, -1) : path;
}

function buildUrls() {
  const config = vscode.workspace.getConfiguration("purdueaf");
  const hubBase = normalizeBasePath(process.env.JUPYTERHUB_BASE_URL || "/");
  const servicePrefix = process.env.JUPYTERHUB_SERVICE_PREFIX || "";
  const labPath =
    config.get("jupyterLabPath") ||
    `${servicePrefix}lab/tree`.replace(/^\/+/, "/");
  const shutdownPath =
    config.get("shutdownPath") || "/hub/home";
  const labUrl = labPath.startsWith("http")
    ? labPath
    : `${hubBase}${labPath.startsWith("/") ? labPath : `/${labPath}`}`;
  const shutdownUrl = shutdownPath.startsWith("http")
    ? shutdownPath
    : `${hubBase}${shutdownPath.startsWith("/") ? shutdownPath : `/${shutdownPath}`}`;
  return { labUrl, shutdownUrl };
}

function openExternal(url) {
  return vscode.env.openExternal(vscode.Uri.parse(url));
}

function activate(context) {
  const { labUrl, shutdownUrl } = buildUrls();

  const switchToJupyterLab = vscode.commands.registerCommand(
    "purdueaf.switchToJupyterLab",
    async () => {
      await openExternal(labUrl);
    }
  );

  const openShutdownPage = vscode.commands.registerCommand(
    "purdueaf.openShutdownPage",
    async () => {
      await openExternal(shutdownUrl);
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
