import { Suspense, useRef } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Stage, useGLTF, Center, Text3D } from "@react-three/drei";
import styles from "../index.module.less";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface ShapeDef {
  shape: "box" | "sphere" | "cylinder" | "cone" | "torus" | "plane";
  position?: [number, number, number];
  rotation?: [number, number, number];
  scale?: [number, number, number];
  color?: string;
  args?: number[];
}

interface Scene3DBlockProps {
  block: {
    title?: string;
    /** URL to a GLTF/GLB 3D model */
    modelUrl?: string;
    /** Primitive shapes to render */
    shapes?: ShapeDef[];
    /** 3D text to display */
    text?: string;
    /** Background color */
    background?: string;
    height?: number;
    /** Camera position */
    cameraPosition?: [number, number, number];
  };
}

function GLTFModel({ url }: { url: string }) {
  const { scene } = useGLTF(url);
  return <primitive object={scene} />;
}

function Shape({ def }: { def: ShapeDef }) {
  const { shape, position = [0, 0, 0], rotation = [0, 0, 0], scale = [1, 1, 1], color = "#4096ff", args } = def;

  const geometryMap: Record<string, JSX.Element> = {
    box: <boxGeometry args={(args as any) || [1, 1, 1]} />,
    sphere: <sphereGeometry args={(args as any) || [0.5, 32, 32]} />,
    cylinder: <cylinderGeometry args={(args as any) || [0.5, 0.5, 1, 32]} />,
    cone: <coneGeometry args={(args as any) || [0.5, 1, 32]} />,
    torus: <torusGeometry args={(args as any) || [0.5, 0.2, 16, 48]} />,
    plane: <planeGeometry args={(args as any) || [2, 2]} />,
  };

  const geometry = geometryMap[shape];
  if (!geometry) return null;

  return (
    <mesh
      position={position}
      rotation={rotation}
      scale={scale}
    >
      {geometry}
      <meshStandardMaterial color={color} />
    </mesh>
  );
}

function SceneContent({ block }: { block: Scene3DBlockProps["block"] }) {
  return (
    <Stage adjustCamera={!block.cameraPosition} intensity={0.5}>
      {block.modelUrl && (
        <Center>
          <GLTFModel url={block.modelUrl} />
        </Center>
      )}
      {block.shapes?.map((shapeDef, i) => (
        <Shape key={i} def={shapeDef} />
      ))}
    </Stage>
  );
}

export default function Scene3DBlock({ block }: Scene3DBlockProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const height = block.height || 400;
  const bg = block.background || "#f0f2f5";

  return (
    <div className={styles.scene3dBlock} ref={containerRef}>
      {block.title && <div className={styles.scene3dTitle}>{block.title}</div>}
      <div className={styles.scene3dCanvas} style={{ height }}>
        <Canvas
          camera={{
            position: block.cameraPosition || [3, 3, 3],
            fov: 50,
          }}
          style={{ background: bg, borderRadius: 10 }}
        >
          <ambientLight intensity={0.4} />
          <directionalLight position={[5, 5, 5]} intensity={0.6} />
          <Suspense fallback={null}>
            <SceneContent block={block} />
          </Suspense>
          <OrbitControls
            enablePan
            enableZoom
            enableRotate
            autoRotate={false}
          />
        </Canvas>
      </div>
      <div className={styles.scene3dHint}>
        Drag to rotate / Scroll to zoom / Right-click to pan
      </div>
    </div>
  );
}
