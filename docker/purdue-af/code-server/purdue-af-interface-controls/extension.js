const vscode = require("vscode");

function normalizeBasePath(path) {
  if (!path || path === "/") {
    return "";
  }
  return path.endsWith("/") ? path.slice(0, -1) : path;
}

function joinUrl(origin, path) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${origin}${normalizedPath}`;
}

function getOrigin(config) {
  const configured = (config.get("hubOrigin") || "").trim();
  if (configured) {
    return normalizeBasePath(configured);
  }
  const remote = vscode.env.remoteName;
  if (remote) {
    return `https://${remote}`;
  }
  return "";
}

function getServicePrefix(config) {
  return (
    config.get("servicePrefix") ||
    process.env.JUPYTERHUB_SERVICE_PREFIX ||
    ""
  );
}

function buildUrls() {
  const config = vscode.workspace.getConfiguration("purdueaf");
  const origin = getOrigin(config);
  const servicePrefix = getServicePrefix(config);
  const labPath = config.get("jupyterLabPath") || `${servicePrefix}lab/tree`;
  const shutdownApiPath =
    config.get("shutdownApiPath") || `${servicePrefix}api/shutdown`;
  const hubHomePath = config.get("hubHomePath") || "/hub/home";

  return {
    origin,
    servicePrefix,
    labUrl: labPath.startsWith("http") ? labPath : joinUrl(origin, labPath),
    shutdownApiUrl: shutdownApiPath.startsWith("http")
      ? shutdownApiPath
      : joinUrl(origin, shutdownApiPath),
    hubHomeUrl: hubHomePath.startsWith("http")
      ? hubHomePath
      : joinUrl(origin, hubHomePath),
  };
}

async function openInBrowser(url, label) {
  const opened = await vscode.env.openExternal(vscode.Uri.parse(url));
  if (opened) {
    return;
  }

  const choice = await vscode.window.showInformationMessage(
    `${label}: open in a new browser tab?`,
    "Open",
    "Copy URL"
  );
  if (choice === "Open") {
    await vscode.env.openExternal(vscode.Uri.parse(url));
    return;
  }
  if (choice === "Copy URL") {
    await vscode.env.clipboard.writeText(url);
    vscode.window.showInformationMessage("URL copied to clipboard.");
  }
}

async function switchToJupyterLab(labUrl) {
  await openInBrowser(labUrl, "Switch to JupyterLab");
}

async function shutdownSession(shutdownApiUrl, hubHomeUrl) {
  const confirmed = await vscode.window.showWarningMessage(
    "Shut down Analysis Facility session? Unsaved data will be lost.",
    { modal: true },
    "Shut Down"
  );
  if (confirmed !== "Shut Down") {
    return;
  }

  try {
    const response = await fetch(shutdownApiUrl, {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
  } catch (error) {
    vscode.window.showErrorMessage(
      `Failed to shut down session: ${error.message || error}`
    );
    return;
  }

  const choice = await vscode.window.showInformationMessage(
    "Session closed. Open JupyterHub home?",
    "Open Hub Home"
  );
  if (choice === "Open Hub Home") {
    await openInBrowser(hubHomeUrl, "JupyterHub home");
  }
}

function activate(context) {
  const urls = buildUrls();

  const switchToJupyterLabCommand = vscode.commands.registerCommand(
    "purdueaf.switchToJupyterLab",
    async () => {
      await switchToJupyterLab(urls.labUrl);
    }
  );

  const openShutdownPage = vscode.commands.registerCommand(
    "purdueaf.openShutdownPage",
    async () => {
      await shutdownSession(urls.shutdownApiUrl, urls.hubHomeUrl);
    }
  );

  const labButton = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    200
  );
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
  shutdownButton.tooltip = "Shut down this Analysis Facility session";
  shutdownButton.command = "purdueaf.openShutdownPage";
  shutdownButton.show();

  context.subscriptions.push(
    switchToJupyterLabCommand,
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
