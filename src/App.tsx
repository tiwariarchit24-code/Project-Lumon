import CommandPage from "./pages/CommandPage";
import BootSplash from "./components/BootSplash";

// The top-level component.
//
// The workstation is mounted immediately, so the map and the data requests
// start at once. The short start-up animation is drawn on top of it and
// removes itself; it never delays the app.
export default function App() {
  return (
    <>
      <CommandPage />
      <BootSplash />
    </>
  );
}
