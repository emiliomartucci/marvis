import path from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";
import js from "@eslint/js";
import sonarjs from "eslint-plugin-sonarjs";
import reactYouMightNotNeedAnEffect from "eslint-plugin-react-you-might-not-need-an-effect";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const compat = new FlatCompat({
  baseDirectory: __dirname,
  recommendedConfig: js.configs.recommended,
});

export default [
  ...compat.extends("next/core-web-vitals", "next/typescript"),

  sonarjs.configs.recommended,

  reactYouMightNotNeedAnEffect.configs.recommended,

  {
    ignores: [".next/**", "out/**", "build/**", "next-env.d.ts"],
  },

  {
    rules: {
      // --- sonarjs: disable rules irrelevant for frontend ---
      "sonarjs/aws-apigateway-public-api": "off",
      "sonarjs/aws-ec2-rds-dms-public": "off",
      "sonarjs/aws-ec2-unencrypted-ebs-volume": "off",
      "sonarjs/aws-efs-unencrypted": "off",
      "sonarjs/aws-iam-all-privileges": "off",
      "sonarjs/aws-iam-all-resources-accessible": "off",
      "sonarjs/aws-iam-privilege-escalation": "off",
      "sonarjs/aws-iam-public-access": "off",
      "sonarjs/aws-opensearchservice-domain": "off",
      "sonarjs/aws-rds-unencrypted-databases": "off",
      "sonarjs/aws-restricted-ip-admin-access": "off",
      "sonarjs/aws-s3-bucket-granted-access": "off",
      "sonarjs/aws-s3-bucket-insecure-http": "off",
      "sonarjs/aws-s3-bucket-public-access": "off",
      "sonarjs/aws-s3-bucket-server-encryption": "off",
      "sonarjs/aws-s3-bucket-versioning": "off",
      "sonarjs/aws-sagemaker-unencrypted-notebook": "off",
      "sonarjs/no-clear-text-protocols": "off",
      "sonarjs/publicly-writable-directories": "off",
      "sonarjs/weak-ssl": "off",
      "sonarjs/cors": "off",
    },
  },
];
