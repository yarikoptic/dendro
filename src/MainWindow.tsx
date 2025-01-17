import { FunctionComponent, useState } from "react";
import ApplicationBar, { applicationBarHeight } from "./ApplicationBar";
import AboutPage from "./pages/AboutPage/AboutPage";
import HomePage from "./pages/HomePage/HomePage";
import useRoute from "./useRoute";
import useWindowDimensions from "./useWindowDimensions";
import DandiBrowser from "./pages/DandiBrowser/DandiBrowser";
import ProjectPage from "./pages/ProjectPage/ProjectPage";
import RegisterComputeResourcePage from "./pages/RegisterComputeResourcePage/RegisterComputeResourcePage";
import ComputeResourcePage from "./pages/ComputeResourcePage/ComputeResourcePage";
import ComputeResourcesPage from "./pages/ComputeResourcesPage/ComputeResourcesPage";
import GitHubAuthPage from "./GitHub/GitHubAuthPage";
import ProjectsPage from "./pages/ProjectsPage/ProjectsPage";
import VBoxLayout from "./components/VBoxLayout";
import HBoxLayout from "./components/HBoxLayout";
import HelpPanel from "./HelpPanel/HelpPanel";
import AdminPage from "./pages/AdminPage/AdminPage";

type Props = {
    // none
}

const MainWindow: FunctionComponent<Props> = () => {
    const {width, height} = useWindowDimensions()
    const H1 = applicationBarHeight
    const H2 = height - applicationBarHeight
    return (
        <VBoxLayout
            width={width}
            heights={[H1, H2]}
        >
            <ApplicationBar />
            <MainContent
                width={0} // filled in by VBoxLayout
                height={0} // filled in by VBoxLayout
            />
        </VBoxLayout>
    )
}

type MainContentProps = {
    width: number
    height: number
}

const MainContent: FunctionComponent<MainContentProps> = ({width, height}) => {
    const [helpExpanded, setHelpExpanded] = useState(true)
    const helpWidth = helpExpanded ? calculateHelpWidth(width) : 30

    return (
        <HBoxLayout
            widths={[width - helpWidth, helpWidth]}
            height={height}
        >
            <MainContent2
                width={0}
                height={0}
            />
            <HelpPanel
                width={0}
                height={0}
                helpExpanded={helpExpanded}
                setHelpExpanded={setHelpExpanded}
            />
        </HBoxLayout>
    )
}

type MainContent2Props = {
    width: number
    height: number
}

const MainContent2: FunctionComponent<MainContent2Props> = ({width, height}) => {
    const {route} = useRoute()
    return (
        (route.page === 'dandisets' || route.page === 'dandiset') ? (
            <DandiBrowser width={width} height={height} />
        ) : route.page === 'project' ? (
            <ProjectPage width={width} height={height} />
        ) : route.page === 'about' ? (
            <AboutPage width={width} height={height} />
        ) : route.page === 'compute-resource' ? (
            <ComputeResourcePage
                width={width}
                height={height}
                computeResourceId={route.computeResourceId}
            />
        ) : route.page === 'compute-resources' ? (
            <ComputeResourcesPage width={width} height={height} />
        ) : route.page === 'projects' ? (
            <ProjectsPage width={width} height={height} />
        ) : route.page === 'register-compute-resource' ? (
            <RegisterComputeResourcePage />
        ) : route.page === 'github-auth' ? (
            <GitHubAuthPage />
        ) : route.page === 'admin' ? (
            <AdminPage width={width} height={height} />
        ) : (
            <div>404</div>
        )
    )
}

const calculateHelpWidth = (width: number) => {
    if (width < 800) {
        return 0
    }
    else if (width < 1200) {
        return 250
    }
    else {
        return 350
    }
}

export default MainWindow