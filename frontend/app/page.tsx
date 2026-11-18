import Workspace from "./workspace";
export default function Home() {
  return (
    <>
      <a className="skip-link" href="#workspace-content">
        Skip to workspace
      </a>
      <main id="workspace-content">
        <Workspace />
      </main>
    </>
  );
}
