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
  const labPath = config.get("jupyterLabPath") || `${servicePrefix}lab`;
  const hubHomePath = config.get("hubHomePath") || "/hub/home";

  return {
    labUrl: labPath.startsWith("http") ? labPath : joinUrl(origin, labPath),
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

function activate(context) {
  const urls = buildUrls();

  const switchToJupyterLab = vscode.commands.registerCommand(
    "purdueaf.switchToJupyterLab",
    async () => {
      await openInBrowser(urls.labUrl, "Switch to JupyterLab");
    }
  );

  const openHubHome = vscode.commands.registerCommand(
    "purdueaf.openShutdownPage",
    async () => {
      await openInBrowser(urls.hubHomeUrl, "Open JupyterHub home");
    }
  );

  const labButton = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    -10000
  );
  labButton.name = "Purdue AF JupyterLab Button";
  labButton.text = "$(purdueaf-jupyter) JupyterLab";
  labButton.tooltip = "Switch to JupyterLab interface";
  labButton.command = "purdueaf.switchToJupyterLab";
  labButton.show();

  const shutdownButton = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    -10001
  );
  shutdownButton.name = "Purdue AF Shutdown Button";
  shutdownButton.text = "$(purdueaf-shutdown) Shut Down";
  shutdownButton.tooltip = "Open JupyterHub home to stop this server";
  shutdownButton.command = "purdueaf.openShutdownPage";
  shutdownButton.show();

  context.subscriptions.push(
    switchToJupyterLab,
    openHubHome,
    labButton,
    shutdownButton
  );
}

function deactivate() {}

module.exports = {
  activate,
  deactivate,
};
