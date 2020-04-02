stage('Run integration tests') {
  stages {
    stage('Deploy test environment') {
      steps {
        script {
          {% include "snippets/get_paas_domain.groovy" %}
          if (!env.PIPELINE_ID) {
            env.PIPELINE_ID = "c3i-mbs-${UUID.randomUUID().toString().take(8)}"
          }
          openshift.withCluster() {
            openshift.withProject(params.PIPELINE_AS_A_SERVICE_BUILD_NAMESPACE) {
              c3i.buildAndWait(script: this, objs: "bc/pipeline-as-a-service",
                '-e', "DEFAULT_IMAGE_TAG=${env.ENVIRONMENT}",
                '-e', "PIPELINE_ID=${env.PIPELINE_ID}",
                '-e', "SERVICES_TO_DEPLOY='umb mbs-frontend mbs-backend krb5 ldap koji-hub'",
                '-e', "C3IAAS_PROJECT=${env.C3IAAS_PROJECT ?: ''}",
                '-e', "MBS_BACKEND_IMAGE=${env.BACKEND_IMAGE_REF}",
                '-e', "MBS_FRONTEND_IMAGE=${env.FRONTEND_IMAGE_REF}",
                '-e', "PAAS_DOMAIN=${env.PAAS_DOMAIN}"
              )
            }
          }
        }
      }
    }
    stage('Run tests') {
      steps {
        script {
          sh "openshift/integration/koji/pipelines/tests/runtests ${env.PIPELINE_ID}"
        }
      }
      post {
        success {
          echo "All tests successful"
          script {
            [env.BACKEND_IMAGE_REF, env.FRONTEND_IMAGE_REF].each {
              sendToResultsDB(it, 'passed')
            }
          }
        }
        failure {
          echo "Testcases FAILED"
        }
      }
    }
  }
  post {
    failure {
      script {
        [env.BACKEND_IMAGE_REF, env.FRONTEND_IMAGE_REF].each {
          sendToResultsDB(it, 'failed')
        }
        openshift.withCluster() {
          openshift.withProject(env.PIPELINE_ID) {
            echo 'Getting logs from all deployments...'
            openshift.selector('pods', ['c3i.redhat.com/pipeline': env.PIPELINE_ID]).logs('--tail 100')
          }
        }
      }
    }
    cleanup {
      script {
        if (params.CLEANUP == 'true' && params.USE_C3IAAS != 'true') {
          openshift.withCluster() {
            openshift.withProject(env.PIPELINE_ID) {
              /* Tear down everything we just created */
              echo 'Tearing down test resources...'
              openshift.selector('all,pvc,configmap,secret',
                                 ['c3i.redhat.com/pipeline': env.PIPELINE_ID]).delete('--ignore-not-found=true')
            }
          }
        } else {
          echo 'Skipping cleanup'
        }
      }
    }
  }
}
